#!/usr/bin/env python3
import json
import os
import re
import secrets
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import requests
import yaml

TOKEN_FILE = Path(".spotify-token.json")
CONFIG_FILE = Path("config.yaml")
STATE_FILE = Path("state.json")


class DailyDriveError(Exception):
    pass


def load_env_file(path: str = ".env") -> None:
    env_path = Path(path)
    if not env_path.exists():
      return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def load_config() -> dict[str, Any]:
    if not CONFIG_FILE.exists():
        raise DailyDriveError("config.yaml not found! Run: cp config.example.yaml config.yaml")
    with CONFIG_FILE.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def load_token() -> dict[str, Any]:
    if not TOKEN_FILE.exists():
        raise DailyDriveError("Not authenticated! Run: python3 setup.py")
    return json.loads(TOKEN_FILE.read_text(encoding="utf-8"))


def save_token(token_data: dict[str, Any]) -> None:
    TOKEN_FILE.write_text(json.dumps(token_data, indent=2), encoding="utf-8")


def load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_state(state_data: dict[str, Any]) -> None:
    STATE_FILE.write_text(json.dumps(state_data, indent=2), encoding="utf-8")


def current_time_from_env():
    from datetime import datetime

    raw = os.environ.get("DAILYDRIVE_NOW")
    if not raw:
        return datetime.now().astimezone()
    candidate = raw.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise DailyDriveError(f'Invalid DAILYDRIVE_NOW value "{raw}"') from exc
    if parsed.tzinfo is None:
        return parsed.astimezone()
    return parsed


class SpotifyClient:
    auth_base = "https://accounts.spotify.com"
    api_base = "https://api.spotify.com/v1"

    def __init__(self, client_id: str, client_secret: str, redirect_uri: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.session = requests.Session()
        self.access_token: str | None = None
        self.refresh_token: str | None = None

    def set_tokens(self, access_token: str | None = None, refresh_token: str | None = None) -> None:
        if access_token:
            self.access_token = access_token
        if refresh_token:
            self.refresh_token = refresh_token

    def create_authorize_url(self, scopes: list[str], state: str | None = None) -> str:
        params = {
            "client_id": self.client_id,
            "response_type": "code",
            "redirect_uri": self.redirect_uri,
            "scope": " ".join(scopes),
            "state": state or secrets.token_urlsafe(12),
        }
        return f"{self.auth_base}/authorize?{urlencode(params)}"

    def authorization_code_grant(self, code: str) -> dict[str, Any]:
        response = self._request(
            "POST",
            f"{self.auth_base}/api/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.redirect_uri,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
            timeout=30,
        )
        return self._parse_json(response, "authorization code grant")

    def refresh_access_token(self) -> dict[str, Any]:
        if not self.refresh_token:
            raise DailyDriveError("Missing refresh token")
        response = self._request(
            "POST",
            f"{self.auth_base}/api/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
            timeout=30,
        )
        return self._parse_json(response, "token refresh")

    def api_request(self, method: str, path: str, *, params: dict[str, Any] | None = None,
                    json_body: Any = None) -> Any:
        if not self.access_token:
            raise DailyDriveError("Spotify access token is missing")
        response = self._request(
            method,
            f"{self.api_base}{path}",
            params=params,
            json=json_body,
            headers={"Authorization": f"Bearer {self.access_token}"},
            timeout=60,
        )
        return self._parse_json(response, f"{method} {path}")

    def get_show_episodes(self, show_id: str, limit: int, market: str = "US") -> Any:
        return self.api_request("GET", f"/shows/{show_id}/episodes", params={"limit": limit, "market": market})

    def get_my_top_tracks(self, limit: int, offset: int, time_range: str) -> Any:
        return self.api_request(
            "GET",
            "/me/top/tracks",
            params={"limit": limit, "offset": offset, "time_range": time_range},
        )

    def get_my_top_artists(self, limit: int, time_range: str) -> Any:
        return self.api_request(
            "GET",
            "/me/top/artists",
            params={"limit": limit, "time_range": time_range},
        )

    def search_tracks(self, query: str, limit: int, market: str = "US") -> Any:
        return self.api_request(
            "GET",
            "/search",
            params={"q": query, "type": "track", "limit": limit, "market": market},
        )

    def playlist_items(self, playlist_id: str, limit: int, offset: int) -> Any:
        return self.api_request(
            "GET",
            f"/playlists/{playlist_id}/items",
            params={"limit": limit, "offset": offset},
        )

    def replace_playlist_items(self, playlist_id: str, uris: list[str]) -> None:
        response = self._request(
            "PUT",
            f"{self.api_base}/playlists/{playlist_id}/items",
            json={"uris": uris},
            headers={"Authorization": f"Bearer {self.access_token}"},
            timeout=60,
        )
        self._parse_json(response, f"PUT /playlists/{playlist_id}/items", allow_empty=True)

    def append_playlist_items(self, playlist_id: str, uris: list[str]) -> None:
        response = self._request(
            "POST",
            f"{self.api_base}/playlists/{playlist_id}/items",
            json={"uris": uris},
            headers={"Authorization": f"Bearer {self.access_token}"},
            timeout=60,
        )
        self._parse_json(response, f"POST /playlists/{playlist_id}/items", allow_empty=True)

    def update_playlist_details(self, playlist_id: str, *, description: str | None = None) -> None:
        body: dict[str, Any] = {}
        if description is not None:
            body["description"] = description
        if not body:
            return
        response = self._request(
            "PUT",
            f"{self.api_base}/playlists/{playlist_id}",
            json=body,
            headers={"Authorization": f"Bearer {self.access_token}"},
            timeout=60,
        )
        self._parse_json(response, f"PUT /playlists/{playlist_id}", allow_empty=True)

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        try:
            return self.session.request(method, url, **kwargs)
        except requests.RequestException as exc:
            raise DailyDriveError(f"Network request failed for {url}: {exc}") from exc

    @staticmethod
    def _parse_json(response: requests.Response, context: str, allow_empty: bool = False) -> Any:
        if not response.ok:
            body = response.text.strip()
            raise DailyDriveError(f"{context} failed: HTTP {response.status_code} {body}")
        if allow_empty and not response.text.strip():
            return {}
        if not response.text.strip():
            return {}
        return response.json()


def ensure_spotify_credentials(config: dict[str, Any]) -> tuple[str, str, str]:
    spotify = config.get("spotify") or {}
    client_id = spotify.get("client_id")
    client_secret = spotify.get("client_secret")
    redirect_uri = spotify.get("redirect_uri")
    if not client_id or not client_secret or client_id == "your-client-id-here":
        raise DailyDriveError("Please fill in your Spotify credentials in config.yaml")
    if not redirect_uri:
        raise DailyDriveError("Missing spotify.redirect_uri in config.yaml")
    return client_id, client_secret, redirect_uri


def refresh_token_if_needed(spotify: SpotifyClient, token: dict[str, Any]) -> dict[str, Any]:
    import time

    expires_at = int(token.get("expires_at", 0))
    if time.time() * 1000 <= expires_at - 5 * 60 * 1000:
        spotify.set_tokens(token.get("access_token"), token.get("refresh_token"))
        return token

    print("🔄 Refreshing access token...")
    spotify.set_tokens(refresh_token=token.get("refresh_token"))
    data = spotify.refresh_access_token()
    token["access_token"] = data["access_token"]
    token["expires_at"] = int(time.time() * 1000) + int(data["expires_in"]) * 1000
    if data.get("refresh_token"):
        token["refresh_token"] = data["refresh_token"]
    save_token(token)
    spotify.set_tokens(token["access_token"], token["refresh_token"])
    print("✅ Token refreshed")
    return token


def strip_code_fences(text: str) -> str:
    return re.sub(r"```ya?ml?\n?|```\n?", "", text, flags=re.IGNORECASE).strip()


def parse_yaml_array(text: str) -> list[str]:
    clean = strip_code_fences(text)
    try:
        data = yaml.safe_load(clean)
        if isinstance(data, list) and data:
            return [str(item).strip() for item in data if str(item).strip()]
    except yaml.YAMLError:
        pass
    items = []
    for line in clean.splitlines():
        candidate = re.sub(r"^-\s*", "", line).strip()
        if candidate and not candidate.startswith("#"):
            items.append(candidate)
    if not items:
        raise DailyDriveError("Failed to parse genres from LLM output")
    return items


def update_genres_in_config(genres: list[str]) -> None:
    raw_config = CONFIG_FILE.read_text(encoding="utf-8")
    genres_yaml = "\n".join(f"    - {genre}" for genre in genres)

    if re.search(r"^  genres:\s*$", raw_config, flags=re.MULTILINE):
        updated = re.sub(
            r"^  genres:\s*\n(    - .*\n)*",
            f"  genres:\n{genres_yaml}\n",
            raw_config,
            flags=re.MULTILINE,
        )
    elif re.search(r"^  # genres:", raw_config, flags=re.MULTILINE):
        updated = re.sub(
            r"^  # genres:\s*\n(  #\s+- .*\n)*",
            f"  genres:\n{genres_yaml}\n",
            raw_config,
            flags=re.MULTILINE,
        )
    else:
        updated = re.sub(
            r"(top_tracks:.*\n(?:    .*\n)*)",
            rf"\1\n  genres:\n{genres_yaml}\n",
            raw_config,
            flags=re.MULTILINE,
        )

    CONFIG_FILE.write_text(updated, encoding="utf-8")
