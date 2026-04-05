#!/usr/bin/env python3
import os

import requests

from daily_drive_common import DailyDriveError, parse_yaml_array, update_genres_in_config
from taste_profile import build_prompt, collect_taste_data
from daily_drive_common import (
    SpotifyClient,
    ensure_spotify_credentials,
    load_config,
    load_env_file,
    load_token,
    refresh_token_if_needed,
)

GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
GEMINI_MODEL = "gemini-2.5-flash"


def main() -> int:
    load_env_file()
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise DailyDriveError(
            "GEMINI_API_KEY not set. Add it to .env or get one from https://aistudio.google.com/apikey"
        )

    print("\n🎵 Taste Profile Generator (Google Gemini — Free)\n")
    config = load_config()
    token = load_token()
    client_id, client_secret, redirect_uri = ensure_spotify_credentials(config)
    spotify = SpotifyClient(client_id, client_secret, redirect_uri)
    spotify.set_tokens(token.get("access_token"), token.get("refresh_token"))
    refresh_token_if_needed(spotify, token)

    top_artists, unique_tracks = collect_taste_data(spotify)
    print(f"\n🎤 Top artists: {', '.join(top_artists[:15])}...")
    print(f"🎵 Unique tracks analyzed: {len(unique_tracks)}")

    try:
        response = requests.post(
            GEMINI_API_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": GEMINI_MODEL, "messages": [{"role": "user", "content": build_prompt(top_artists, unique_tracks)}]},
            timeout=60,
        )
    except requests.RequestException as exc:
        raise DailyDriveError(f"Gemini API request failed: {exc}") from exc
    if not response.ok:
        message = f"Gemini API error: {response.status_code} {response.text}"
        if response.status_code in {400, 403}:
            message += "\nCheck that your GEMINI_API_KEY is valid: https://aistudio.google.com/apikey"
        raise DailyDriveError(message)

    llm_output = response.json()["choices"][0]["message"]["content"].strip()
    print("LLM suggested genres:")
    print(llm_output)
    genres = parse_yaml_array(llm_output)

    print(f"\n✅ Detected {len(genres)} genres:")
    for genre in genres:
        print(f"   - {genre}")

    update_genres_in_config(genres)
    print("\n💾 Updated config.yaml with new genres")
    print("\nRun 'python3 daily_drive.py --dry-run' to preview your updated playlist.\n")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except DailyDriveError as exc:
        print(f"\n❌ Error: {exc}")
        raise SystemExit(1)
