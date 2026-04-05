#!/usr/bin/env python3
import argparse
import random
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from daily_drive_common import (
    DailyDriveError,
    SpotifyClient,
    current_time_from_env,
    ensure_spotify_credentials,
    load_config,
    load_token,
    refresh_token_if_needed,
    save_state,
)


def parse_schedule_time(label: str, value: str) -> int:
    if not isinstance(value, str) or len(value) != 5 or value[2] != ":":
        raise DailyDriveError(f'Invalid {label} schedule time "{value}". Expected HH:MM.')
    hour = int(value[:2])
    minute = int(value[3:])
    if hour > 23 or minute > 59:
        raise DailyDriveError(f'Invalid {label} schedule time "{value}". Expected HH:MM.')
    return hour * 60 + minute


def resolve_refresh_slot(config: dict) -> dict:
    schedule = config.get("schedule") or {}
    times = schedule.get("times") or []
    time_zone = schedule.get("timezone")
    if not time_zone:
        raise DailyDriveError("Missing schedule.timezone in config.yaml")
    if len(times) < 2:
        raise DailyDriveError("schedule.times must contain at least two entries: morning then evening")

    morning_time = times[0]
    evening_time = times[1]
    morning_minutes = parse_schedule_time("morning", morning_time)
    evening_minutes = parse_schedule_time("evening", evening_time)
    if evening_minutes <= morning_minutes:
        raise DailyDriveError(
            f"schedule.times must be ordered morning then evening. Got {morning_time} then {evening_time}."
        )

    try:
        now_local = current_time_from_env().astimezone(ZoneInfo(time_zone))
    except Exception as exc:
        raise DailyDriveError(f'Invalid schedule.timezone "{time_zone}" in config.yaml') from exc

    current_minutes = now_local.hour * 60 + now_local.minute
    slot = "evening" if current_minutes >= evening_minutes else "morning"
    return {
        "slot": slot,
        "timeZone": time_zone,
        "currentTime": now_local.strftime("%H:%M"),
        "currentDate": now_local.strftime("%Y-%m-%d"),
        "morningTime": morning_time,
        "eveningTime": evening_time,
    }


def get_effective_music_config(config: dict, refresh_slot: str) -> dict:
    music_config = config.get("music") or {}
    shared_playlists = list(music_config.get("playlists") or [])
    morning_playlists = list(music_config.get("morning_playlists") or [])
    effective = dict(music_config)
    effective["playlists"] = shared_playlists + morning_playlists if refresh_slot == "morning" else shared_playlists
    return effective


def shuffle_items(items: list[dict]) -> list[dict]:
    shuffled = list(items)
    random.shuffle(shuffled)
    return shuffled


def fetch_podcast_episodes(spotify: SpotifyClient, podcasts: list[dict]) -> list[dict]:
    episodes: list[dict] = []
    for podcast in podcasts:
        count = int(podcast.get("episodes") or 1)
        print(f"🎙️  Fetching {count} episode(s) from: {podcast['name']}")
        try:
            data = spotify.get_show_episodes(podcast["id"], count)
            for episode in data.get("items", []):
                episodes.append(
                    {
                        "uri": episode["uri"],
                        "name": episode["name"],
                        "show": podcast["name"],
                        "type": "episode",
                        "position": podcast.get("position"),
                    }
                )
                print(f"    📌 {episode['name']}")
        except Exception as exc:
            print(f"    ⚠️  Failed to fetch {podcast['name']}: {exc}")
    return episodes


def fetch_music_tracks(spotify: SpotifyClient, music_config: dict) -> list[dict]:
    all_tracks: list[dict] = []
    now = current_time_from_env()

    for playlist in music_config.get("playlists") or []:
        if not playlist.get("id") or playlist["id"] == "your-playlist-id":
            continue
        print(f"🎵 Fetching songs from playlist: {playlist['name']}")
        try:
            offset = 0
            freshest_added_at: datetime | None = None
            playlist_tracks: list[dict] = []
            while True:
                data = spotify.playlist_items(playlist["id"], 100, offset)
                for entry in data.get("items", []):
                    added_at_raw = entry.get("added_at")
                    if added_at_raw:
                        added_at = datetime.fromisoformat(added_at_raw.replace("Z", "+00:00"))
                        if freshest_added_at is None or added_at > freshest_added_at:
                            freshest_added_at = added_at

                    track = entry.get("item")
                    if track and track.get("uri") and track.get("type") == "track":
                        playlist_tracks.append(
                            {
                                "uri": track["uri"],
                                "name": track["name"],
                                "artist": ", ".join(artist["name"] for artist in track.get("artists", [])) or "Unknown",
                                "type": "track",
                            }
                        )

                offset += 100
                if offset >= int(data.get("total", 0)):
                    break

            if freshest_added_at is None:
                print("    ⏭️  Skipping playlist: couldn't determine when it was last updated")
                continue
            if now - freshest_added_at > timedelta(days=1):
                print(
                    f"    ⏭️  Skipping playlist: last track added {freshest_added_at.isoformat()}, more than 24 hours ago"
                )
                continue

            all_tracks.extend(playlist_tracks)
            print(f"    Found {len(playlist_tracks)} fresh tracks from this playlist")
        except Exception as exc:
            print(f"    ⚠️  Failed to fetch playlist {playlist['name']}: {exc}")

    top_tracks_config = music_config.get("top_tracks") or {}
    if top_tracks_config.get("enabled"):
        time_range = top_tracks_config.get("time_range") or "short_term"
        count = int(top_tracks_config.get("count") or 30)
        print(f"🎵 Fetching top tracks ({time_range})...")
        try:
            offset = 0
            remaining = count
            while remaining > 0:
                limit = min(remaining, 50)
                data = spotify.get_my_top_tracks(limit=limit, offset=offset, time_range=time_range)
                items = data.get("items", [])
                for track in items:
                    all_tracks.append(
                        {
                            "uri": track["uri"],
                            "name": track["name"],
                            "artist": ", ".join(artist["name"] for artist in track.get("artists", [])) or "Unknown",
                            "type": "track",
                        }
                    )
                if len(items) < limit:
                    break
                offset += limit
                remaining -= limit
            print(f"    Found {len(all_tracks)} tracks from playlists + top tracks")
        except Exception as exc:
            print(f"    ⚠️  Failed to fetch top tracks: {exc}")

    total_songs = int(music_config.get("total_songs") or 15)
    if music_config.get("shuffle", True):
        all_tracks = shuffle_items(all_tracks)
    all_tracks = all_tracks[:total_songs]
    print(f"🎵 Selected {len(all_tracks)} songs")
    return all_tracks


def fetch_genre_tracks(spotify: SpotifyClient, genres: list[str], count: int) -> list[dict]:
    tracks: list[dict] = []
    per_genre = max(1, -(-count // len(genres)))
    for genre in genres:
        print(f"🎵 Searching for {genre} tracks...")
        try:
            data = spotify.search_tracks(f"genre:{genre}", min(per_genre, 10))
            items = data.get("tracks", {}).get("items", [])
            for track in items:
                tracks.append(
                    {
                        "uri": track["uri"],
                        "name": track["name"],
                        "artist": ", ".join(artist["name"] for artist in track.get("artists", [])) or "Unknown",
                        "type": "track",
                    }
                )
            print(f"    Found {len(items)} tracks")
        except Exception as exc:
            print(f"    ⚠️  Failed to search genre {genre}: {exc}")
    return shuffle_items(tracks)[:count]


def fetch_all_music_tracks(spotify: SpotifyClient, music_config: dict) -> list[dict]:
    total_songs = int(music_config.get("total_songs") or 15)
    genres = music_config.get("genres") or []
    has_genres = len(genres) > 0
    familiar_count = (total_songs + 1) // 2 if has_genres else total_songs
    discovery_count = 5 # total_songs - familiar_count if has_genres else 0

    familiar_config = dict(music_config)
    familiar_config["total_songs"] = familiar_count
    tracks = fetch_music_tracks(spotify, familiar_config)

    if has_genres and discovery_count > 0:
        genre_tracks = fetch_genre_tracks(spotify, genres, discovery_count)
        familiar_uris = {track["uri"] for track in tracks}
        new_genre_tracks = [track for track in genre_tracks if track["uri"] not in familiar_uris]
        tracks.extend(new_genre_tracks[:discovery_count])
        print(
            f"🎵 Music mix: {familiar_count} familiar + {len(new_genre_tracks[:discovery_count])} discovery = {len(tracks)} total"
        )
    return tracks


def mix_content(episodes: list[dict], tracks: list[dict], pattern: str | None) -> list[dict]:
    mixed: list[dict] = []
    episode_index = 0
    track_index = 0
    pattern_index = 0
    mix_pattern = pattern or "PMMM"

    while episode_index < len(episodes) or track_index < len(tracks):
        slot = mix_pattern[pattern_index % len(mix_pattern)]
        if slot in {"P", "p"}:
            if episode_index < len(episodes):
                mixed.append(episodes[episode_index])
                episode_index += 1
        else:
            if track_index < len(tracks):
                mixed.append(tracks[track_index])
                track_index += 1
        pattern_index += 1
        if episode_index >= len(episodes) and track_index < len(tracks):
            mixed.extend(tracks[track_index:])
            break
        if track_index >= len(tracks) and episode_index < len(episodes):
            mixed.extend(episodes[episode_index:])
            break
    return mixed


def update_playlist(spotify: SpotifyClient, playlist_id: str, items: list[dict], dry_run: bool) -> None:
    uris = [item["uri"] for item in items]
    if dry_run:
        print("\n🧪 DRY RUN — would update playlist with:\n")
        for index, item in enumerate(items, start=1):
            icon = "🎙️ " if item["type"] == "episode" else "🎵"
            detail = f"[{item['show']}] {item['name']}" if item["type"] == "episode" else f"{item['name']} — {item['artist']}"
            print(f"  {str(index).rjust(2)}. {icon} {detail}")
        print(f"\n✅ Dry run complete. {len(items)} items would be added.\n")
        return

    spotify.replace_playlist_items(playlist_id, uris[:100])
    for start in range(100, len(uris), 100):
        spotify.append_playlist_items(playlist_id, uris[start:start + 100])

    print(f"\n✅ Playlist updated with {len(items)} items!")
    print(f"   🎙️  {sum(1 for item in items if item['type'] == 'episode')} podcast episodes")
    print(f"   🎵 {sum(1 for item in items if item['type'] == 'track')} songs\n")


def build_playlist_description(refresh_window: dict) -> str:
    now_local = current_time_from_env().astimezone(ZoneInfo(refresh_window["timeZone"]))
    return (
        f"Updated {now_local.strftime('%b %-d, %Y at %-I:%M %p %Z')} "
        f"({refresh_window['slot']} refresh)"
    )[:300]


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the Daily Drive playlist")
    parser.add_argument("--dry-run", action="store_true", help="Preview the playlist without updating Spotify")
    args = parser.parse_args()

    config = load_config()
    refresh_window = resolve_refresh_slot(config)
    token = load_token()
    client_id, client_secret, redirect_uri = ensure_spotify_credentials(config)
    spotify = SpotifyClient(client_id, client_secret, redirect_uri)
    spotify.set_tokens(token.get("access_token"), token.get("refresh_token"))
    refresh_token_if_needed(spotify, token)

    print(f"\n🚗 Daily Drive — {refresh_window['slot']} refresh...\n")
    print(f"🕒 Local time in {refresh_window['timeZone']}: {refresh_window['currentDate']} {refresh_window['currentTime']}")
    print(f"   Morning slot: {refresh_window['morningTime']} | Evening slot: {refresh_window['eveningTime']}")

    playlist_id = config.get("playlist_id")
    if not playlist_id or playlist_id == "your-playlist-id-here":
        raise DailyDriveError("Please set your playlist_id in config.yaml")

    episodes = fetch_podcast_episodes(spotify, config.get("podcasts") or [])
    current_episode_uris = ",".join(sorted(episode["uri"] for episode in episodes))

    effective_music_config = get_effective_music_config(config, refresh_window["slot"])
    if refresh_window["slot"] == "morning" and (config.get("music") or {}).get("morning_playlists"):
        print(f"🌅 Including {len((config.get('music') or {}).get('morning_playlists') or [])} morning-only playlist source(s)")
    tracks = fetch_all_music_tracks(spotify, effective_music_config)

    if not episodes and not tracks:
        raise DailyDriveError("No content found! Check your config.yaml settings.")

    pinned_first = [episode for episode in episodes if episode.get("position") == "first"]
    mixable_episodes = [episode for episode in episodes if episode.get("position") != "first"]

    print(f"\n🔀 Mixing with pattern: {config.get('mix_pattern') or 'PMMM'}")
    mixed = pinned_first + mix_content(mixable_episodes, tracks, config.get("mix_pattern"))

    update_playlist(spotify, playlist_id, mixed, args.dry_run)

    if not args.dry_run:
        description = build_playlist_description(refresh_window)
        spotify.update_playlist_details(playlist_id, description=description)
        print(f"📝 Playlist description updated: {description}")
        save_state(
            {
                "episode_uris": current_episode_uris,
                "last_updated": datetime.now().astimezone().isoformat(),
                "refresh_slot": refresh_window["slot"],
            }
        )
        print("💾 State saved to state.json")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except DailyDriveError as exc:
        print(f"\n❌ Error: {exc}")
        raise SystemExit(1)
