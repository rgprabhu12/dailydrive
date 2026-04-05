#!/usr/bin/env python3
import requests

from daily_drive_common import (
    DailyDriveError,
    SpotifyClient,
    ensure_spotify_credentials,
    load_config,
    load_env_file,
    load_token,
    parse_yaml_array,
    refresh_token_if_needed,
    update_genres_in_config,
)

DEMETERICS_API_URL = "https://api.demeterics.com/chat/v1/chat/completions"
DEMETERICS_MODEL = "google/gemini-2.5-flash"


def collect_taste_data(spotify: SpotifyClient) -> tuple[list[str], list[dict]]:
    artist_counts: dict[str, int] = {}
    track_samples: list[dict] = []

    for time_range in ["short_term", "medium_term", "long_term"]:
        print(f"📊 Fetching top tracks ({time_range})...")
        data = spotify.get_my_top_tracks(limit=50, offset=0, time_range=time_range)
        for track in data.get("items", []):
            track_samples.append(
                {"name": track["name"], "artists": [artist["name"] for artist in track.get("artists", [])]}
            )
            for artist in track.get("artists", []):
                artist_counts[artist["name"]] = artist_counts.get(artist["name"], 0) + 1

    for time_range in ["short_term", "medium_term", "long_term"]:
        print(f"📊 Fetching top artists ({time_range})...")
        data = spotify.get_my_top_artists(limit=50, time_range=time_range)
        for artist in data.get("items", []):
            artist_counts[artist["name"]] = artist_counts.get(artist["name"], 0) + 2

    top_artists = [name for name, _ in sorted(artist_counts.items(), key=lambda item: item[1], reverse=True)[:40]]

    seen: set[str] = set()
    unique_tracks: list[dict] = []
    for track in track_samples:
        key = f"{track['name']}|{track['artists'][0] if track['artists'] else ''}"
        if key not in seen:
            seen.add(key)
            unique_tracks.append(track)

    return top_artists, unique_tracks


def build_prompt(top_artists: list[str], unique_tracks: list[dict]) -> str:
    artist_list = ", ".join(top_artists)
    track_list = "\n".join(
        f"{track['name']} — {', '.join(track['artists'])}" for track in unique_tracks[:50]
    )
    return f"""Based on this Spotify listening data, generate a list of 5-8 genre/style tags that best describe this user's music taste. These tags will be used as Spotify search queries (e.g., "genre:pop") to discover new music matching their taste.

Top artists (ranked by listening frequency):
{artist_list}

Sample tracks:
{track_list}

Requirements:
- Return ONLY a YAML array of strings, nothing else
- Use genres that work well as Spotify search queries
- Be specific enough to be useful (e.g., "synth pop" not just "pop")
- Cover the breadth of their taste, not just the most common genre
- Use lowercase

Example output format:
- synth pop
- indie rock
- electronic
- alt pop
- dance pop"""


def main() -> int:
    load_env_file()
    api_key = __import__("os").environ.get("DEMETERICS_API_KEY")
    if not api_key:
        raise DailyDriveError("DEMETERICS_API_KEY not set. Add it to .env or export it.")

    print("\n🎵 Taste Profile Generator\n")
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
            DEMETERICS_API_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": DEMETERICS_MODEL, "messages": [{"role": "user", "content": build_prompt(top_artists, unique_tracks)}]},
            timeout=60,
        )
    except requests.RequestException as exc:
        raise DailyDriveError(f"Demeterics API request failed: {exc}") from exc
    if not response.ok:
        raise DailyDriveError(f"Demeterics API error: {response.status_code} {response.text}")

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
