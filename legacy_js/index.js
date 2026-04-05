#!/usr/bin/env node
// =============================================================================
// Daily Drive — Main Script
// =============================================================================
// Builds your custom Daily Drive playlist by mixing podcasts and music.
// This recreates Spotify's discontinued "Daily Drive" feature.
//
// Usage:  npm start                  (scheduled refresh — new music + podcasts)
//         npm test                   (dry run — shows what would happen)
//         node index.js --dry-run
// =============================================================================

// --- Node.js built-in modules ---
const fs = require("fs");

// --- Third-party libraries (installed via npm install) ---
const yaml = require("js-yaml");               // Parses YAML config files
const SpotifyWebApi = require("spotify-web-api-node"); // Wraps the Spotify Web API

// --- File paths used by the script ---
const TOKEN_FILE = ".spotify-token.json";  // Stores your Spotify OAuth tokens (created by setup.js)
const CONFIG_FILE = "config.yaml";         // Your configuration (podcasts, music, schedule, etc.)
const STATE_FILE = "state.json";           // Stores last run metadata for visibility/debugging

// Check command-line flags
const DRY_RUN = process.argv.includes("--dry-run");       // Shows what would happen without changing the playlist

// =============================================================================
// Helper Functions
// =============================================================================

/**
 * Loads and parses config.yaml. Exits with an error if the file doesn't exist.
 * This file contains your Spotify credentials, podcast list, music preferences, etc.
 */
function loadConfig() {
  if (!fs.existsSync(CONFIG_FILE)) {
    console.error("❌ config.yaml not found! Run: cp config.example.yaml config.yaml");
    process.exit(1);
  }
  return yaml.load(fs.readFileSync(CONFIG_FILE, "utf8"));
}

/**
 * Loads the saved OAuth token from disk. Exits if not found.
 * The token file is created when you run `npm run setup` for the first time.
 */
function loadToken() {
  if (!fs.existsSync(TOKEN_FILE)) {
    console.error("❌ Not authenticated! Run: npm run setup");
    process.exit(1);
  }
  return JSON.parse(fs.readFileSync(TOKEN_FILE, "utf8"));
}

/**
 * Saves the OAuth token back to disk (called after a token refresh).
 */
function saveToken(tokenData) {
  fs.writeFileSync(TOKEN_FILE, JSON.stringify(tokenData, null, 2));
}

/**
 * Saves the latest run metadata to disk.
 */
function saveState(state) {
  fs.writeFileSync(STATE_FILE, JSON.stringify(state, null, 2));
}

/**
 * Parses a HH:MM schedule string into minutes since midnight.
 */
function parseScheduleTime(label, value) {
  if (typeof value !== "string" || !/^\d{2}:\d{2}$/.test(value)) {
    throw new Error(`Invalid ${label} schedule time "${value}". Expected HH:MM.`);
  }

  const [hoursText, minutesText] = value.split(":");
  const hours = Number(hoursText);
  const minutes = Number(minutesText);

  if (hours > 23 || minutes > 59) {
    throw new Error(`Invalid ${label} schedule time "${value}". Expected HH:MM.`);
  }

  return hours * 60 + minutes;
}

/**
 * Returns the current local time parts for the configured timezone.
 */
function getLocalTimeParts(date, timeZone) {
  const formatter = new Intl.DateTimeFormat("en-US", {
    timeZone,
    hour12: false,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });

  const parts = Object.fromEntries(
    formatter
      .formatToParts(date)
      .filter((part) => part.type !== "literal")
      .map((part) => [part.type, part.value])
  );

  return {
    year: Number(parts.year),
    month: Number(parts.month),
    day: Number(parts.day),
    hour: Number(parts.hour),
    minute: Number(parts.minute),
  };
}

/**
 * Returns the current time, with DAILYDRIVE_NOW support for dry-run testing.
 */
function getCurrentTime() {
  const nowSource = process.env.DAILYDRIVE_NOW ? new Date(process.env.DAILYDRIVE_NOW) : new Date();
  if (Number.isNaN(nowSource.getTime())) {
    throw new Error(`Invalid DAILYDRIVE_NOW value "${process.env.DAILYDRIVE_NOW}"`);
  }
  return nowSource;
}

/**
 * Resolves whether this run should behave like the morning or evening refresh.
 * schedule.times[0] is treated as morning and schedule.times[1] as evening.
 */
function resolveRefreshSlot(config) {
  const schedule = config.schedule || {};
  const times = Array.isArray(schedule.times) ? schedule.times : [];
  const timeZone = schedule.timezone;

  if (!timeZone) {
    throw new Error("Missing schedule.timezone in config.yaml");
  }
  if (times.length < 2) {
    throw new Error("schedule.times must contain at least two entries: morning then evening");
  }

  const morningTime = times[0];
  const eveningTime = times[1];
  const morningMinutes = parseScheduleTime("morning", morningTime);
  const eveningMinutes = parseScheduleTime("evening", eveningTime);

  if (eveningMinutes <= morningMinutes) {
    throw new Error(
      `schedule.times must be ordered morning then evening. Got ${morningTime} then ${eveningTime}.`
    );
  }

  const nowSource = getCurrentTime();

  let local;
  try {
    local = getLocalTimeParts(nowSource, timeZone);
  } catch (err) {
    throw new Error(`Invalid schedule.timezone "${timeZone}" in config.yaml`);
  }
  const currentMinutes = local.hour * 60 + local.minute;
  const slot = currentMinutes >= eveningMinutes ? "evening" : "morning";

  return {
    slot,
    timeZone,
    currentTime: `${String(local.hour).padStart(2, "0")}:${String(local.minute).padStart(2, "0")}`,
    currentDate: `${local.year}-${String(local.month).padStart(2, "0")}-${String(local.day).padStart(2, "0")}`,
    morningTime,
    eveningTime,
  };
}

/**
 * Builds the music config for this run. Morning refreshes can add extra playlists.
 */
function getEffectiveMusicConfig(config, refreshSlot) {
  const musicConfig = config.music || {};
  const sharedPlaylists = Array.isArray(musicConfig.playlists) ? musicConfig.playlists : [];
  const morningPlaylists = Array.isArray(musicConfig.morning_playlists)
    ? musicConfig.morning_playlists
    : [];

  return {
    ...musicConfig,
    playlists: refreshSlot === "morning"
      ? [...sharedPlaylists, ...morningPlaylists]
      : sharedPlaylists,
  };
}

/**
 * Fisher-Yates shuffle — randomizes an array in-place.
 * Used to shuffle music tracks so the playlist feels fresh each time.
 */
function shuffle(array) {
  const arr = [...array]; // Create a copy so we don't modify the original
  for (let i = arr.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [arr[i], arr[j]] = [arr[j], arr[i]]; // Swap elements
  }
  return arr;
}

/**
 * Spotify access tokens expire after 1 hour. This function checks if the token
 * is about to expire (within 5 minutes) and refreshes it automatically using
 * the long-lived refresh token. You don't need to re-authenticate manually.
 */
async function refreshTokenIfNeeded(spotifyApi, token) {
  if (Date.now() > token.expires_at - 5 * 60 * 1000) {
    console.log("🔄 Refreshing access token...");
    const data = await spotifyApi.refreshAccessToken();

    // Update the token in memory
    token.access_token = data.body.access_token;
    token.expires_at = Date.now() + data.body.expires_in * 1000;

    // Spotify sometimes rotates the refresh token too — save it if provided
    if (data.body.refresh_token) {
      token.refresh_token = data.body.refresh_token;
    }

    // Persist to disk and update the API client
    saveToken(token);
    spotifyApi.setAccessToken(token.access_token);
    console.log("✅ Token refreshed");
  }
}

// =============================================================================
// Core Logic
// =============================================================================

/**
 * Fetches the latest episodes for each podcast listed in your config.
 * Returns an array of episode objects with uri, name, show name, and position.
 *
 * Note: Some podcasts (like NPR News Now) publish hourly episodes that expire
 * quickly on Spotify. If you see "[unavailable]" in your playlist, run the
 * script again to fetch the latest episode.
 */
async function fetchPodcastEpisodes(spotifyApi, podcasts) {
  const episodes = [];

  for (const podcast of podcasts) {
    // How many recent episodes to grab (default: 1, configurable per podcast)
    const count = podcast.episodes || 1;
    console.log(`🎙️  Fetching ${count} episode(s) from: ${podcast.name}`);

    try {
      // Ask Spotify for the most recent episodes of this show
      const data = await spotifyApi.getShowEpisodes(podcast.id, {
        limit: count,
        market: "US", // Required for episode availability
      });

      for (const episode of data.body.items) {
        episodes.push({
          uri: episode.uri,      // Spotify URI like "spotify:episode:abc123"
          name: episode.name,
          show: podcast.name,
          type: "episode",
          position: podcast.position || null, // "first" = pinned to top of playlist
        });
        console.log(`    📌 ${episode.name}`);
      }
    } catch (err) {
      // Don't crash if one podcast fails — just warn and continue with the rest
      console.error(`    ⚠️  Failed to fetch ${podcast.name}: ${err.message}`);
    }
  }

  return episodes;
}

/**
 * Fetches music tracks from two "familiar" sources:
 *   1. Source playlists — songs from playlists you specify in config.yaml
 *   2. Top tracks — your most-played songs on Spotify
 *
 * Tracks are shuffled and trimmed to the requested count.
 */
async function fetchMusicTracks(spotifyApi, musicConfig) {
  let allTracks = [];
  const oneDayMs = 24 * 60 * 60 * 1000;
  const now = getCurrentTime();

  // --- Source 1: Pull tracks from user-specified playlists ---
  if (musicConfig.playlists) {
    for (const playlist of musicConfig.playlists) {
      // Skip placeholder entries from the example config
      if (!playlist.id || playlist.id === "your-playlist-id") continue;

      console.log(`🎵 Fetching songs from playlist: ${playlist.name}`);

      try {
        // Spotify returns max 100 items per request, so we paginate through
        // larger playlists by incrementing the offset
        const accessToken = spotifyApi.getAccessToken();
        let offset = 0;
        let hasMore = true;
        let freshestAddedAt = null;
        const playlistTracks = [];

        while (hasMore) {
          // IMPORTANT: We use the /items endpoint directly via fetch() because
          // the spotify-web-api-node library's getPlaylistTracks() still hits
          // the old /tracks endpoint, which Spotify deprecated in Feb 2026 and
          // now returns 403 Forbidden.
          const res = await fetch(
            `https://api.spotify.com/v1/playlists/${playlist.id}/items?limit=100&offset=${offset}`,
            { headers: { Authorization: `Bearer ${accessToken}` } }
          );

          if (!res.ok) {
            throw new Error(`HTTP ${res.status}: ${await res.text()}`);
          }

          const data = await res.json();

          for (const entry of data.items) {
            if (entry.added_at) {
              const addedAt = new Date(entry.added_at);
              if (!Number.isNaN(addedAt.getTime()) && (!freshestAddedAt || addedAt > freshestAddedAt)) {
                freshestAddedAt = addedAt;
              }
            }

            // The /items endpoint returns the content in entry.item (not entry.track)
            const track = entry.item;
            if (track && track.uri && track.type === "track") {
              playlistTracks.push({
                uri: track.uri,
                name: track.name,
                artist: track.artists?.map((a) => a.name).join(", ") || "Unknown",
                type: "track",
              });
            }
          }

          offset += 100;
          hasMore = offset < data.total;
        }

        if (!freshestAddedAt) {
          console.log("    ⏭️  Skipping playlist: couldn't determine when it was last updated");
          continue;
        }

        if (now.getTime() - freshestAddedAt.getTime() > oneDayMs) {
          console.log(
            `    ⏭️  Skipping playlist: last track added ${freshestAddedAt.toISOString()}, more than 24 hours ago`
          );
          continue;
        }

        allTracks.push(...playlistTracks);
        console.log(`    Found ${playlistTracks.length} fresh tracks from this playlist`);
      } catch (err) {
        console.error(
          `    ⚠️  Failed to fetch playlist ${playlist.name}: ${err.message}`
        );
      }
    }
  }

  // --- Source 2: Pull from user's top tracks (most-played songs) ---
  if (musicConfig.top_tracks && musicConfig.top_tracks.enabled) {
    // time_range controls the window:
    //   "short_term"  = last ~4 weeks
    //   "medium_term" = last ~6 months
    //   "long_term"   = all time
    const timeRange = musicConfig.top_tracks.time_range || "short_term";
    const count = musicConfig.top_tracks.count || 30;
    console.log(`🎵 Fetching top tracks (${timeRange})...`);

    try {
      let offset = 0;
      let remaining = count;

      // Spotify returns max 50 top tracks per request, so paginate if needed
      while (remaining > 0) {
        const limit = Math.min(remaining, 50);
        const data = await spotifyApi.getMyTopTracks({ limit, offset, time_range: timeRange });

        for (const track of data.body.items) {
          allTracks.push({
            uri: track.uri,
            name: track.name,
            artist: track.artists?.map((a) => a.name).join(", ") || "Unknown",
            type: "track",
          });
        }

        // If we got fewer tracks than requested, there are no more
        if (data.body.items.length < limit) break;
        offset += limit;
        remaining -= limit;
      }

      console.log(`    Found ${allTracks.length} tracks from top tracks`);
    } catch (err) {
      console.error(`    ⚠️  Failed to fetch top tracks: ${err.message}`);
    }
  }

  // Shuffle and trim to the desired total number of songs
  const totalSongs = musicConfig.total_songs || 15;
  if (musicConfig.shuffle !== false) {
    allTracks = shuffle(allTracks);
  }
  allTracks = allTracks.slice(0, totalSongs);

  console.log(`🎵 Selected ${allTracks.length} songs`);
  return allTracks;
}

/**
 * Fetches "discovery" tracks by searching Spotify for songs matching your
 * configured genres (e.g., "dance pop", "indie rock"). This helps you discover
 * new music outside your usual listening habits.
 *
 * Tracks are split evenly across genres, then shuffled and trimmed.
 */
async function fetchGenreTracks(spotifyApi, genres, count) {
  const tracks = [];
  // Divide the target count evenly among configured genres
  const perGenre = Math.ceil(count / genres.length);

  for (const genre of genres) {
    console.log(`🎵 Searching for ${genre} tracks...`);
    try {
      // Use Spotify's search with a "genre:" filter
      const data = await spotifyApi.searchTracks(`genre:${genre}`, {
        limit: Math.min(perGenre, 10), // Spotify Dev Mode caps search at 10 results per query
        market: "US",
      });

      for (const track of data.body.tracks.items) {
        tracks.push({
          uri: track.uri,
          name: track.name,
          artist: track.artists?.map((a) => a.name).join(", ") || "Unknown",
          type: "track",
        });
      }
      console.log(`    Found ${data.body.tracks.items.length} tracks`);
    } catch (err) {
      console.error(`    ⚠️  Failed to search genre ${genre}: ${err.message}`);
    }
  }

  // Shuffle so we don't always get the same top results, then trim to count
  return shuffle(tracks).slice(0, count);
}

/**
 * Interleaves podcast episodes and music tracks according to a pattern string.
 *
 * Pattern example: "PMMM" means: 1 podcast, 3 music, 1 podcast, 3 music, ...
 *   P = podcast episode slot
 *   M = music track slot
 *
 * The pattern repeats cyclically. When one content type runs out, the remaining
 * items of the other type are appended at the end.
 */
function mixContent(episodes, tracks, pattern) {
  const mixed = [];
  let episodeIndex = 0;
  let trackIndex = 0;
  let patternIndex = 0;

  const mixPattern = pattern || "PMMM";

  // Walk through the pattern, placing content in the appropriate slots
  while (episodeIndex < episodes.length || trackIndex < tracks.length) {
    // Which slot are we on? The pattern repeats using modulo (%)
    const slot = mixPattern[patternIndex % mixPattern.length];

    if (slot === "P" || slot === "p") {
      // Podcast slot — place next episode if available
      if (episodeIndex < episodes.length) {
        mixed.push(episodes[episodeIndex++]);
      }
    } else {
      // Music slot (M) — place next track if available
      if (trackIndex < tracks.length) {
        mixed.push(tracks[trackIndex++]);
      }
    }

    patternIndex++;

    // Safety valve: if one type is exhausted, dump all remaining items of the other
    // This prevents an infinite loop when the pattern asks for content we don't have
    if (episodeIndex >= episodes.length && trackIndex < tracks.length) {
      while (trackIndex < tracks.length) {
        mixed.push(tracks[trackIndex++]);
      }
      break;
    }
    if (trackIndex >= tracks.length && episodeIndex < episodes.length) {
      while (episodeIndex < episodes.length) {
        mixed.push(episodes[episodeIndex++]);
      }
      break;
    }
  }

  return mixed;
}

/**
 * Replaces the entire playlist with the given items.
 *
 * Uses the Spotify /items endpoint (NOT /tracks, which was deprecated in Feb 2026).
 * PUT replaces the first 100 items; POST appends additional batches if needed.
 * This endpoint accepts both track and episode URIs.
 */
async function updatePlaylist(spotifyApi, playlistId, items) {
  const uris = items.map((item) => item.uri);

  // In dry-run mode, just print what would happen and return
  if (DRY_RUN) {
    console.log("\n🧪 DRY RUN — would update playlist with:\n");
    items.forEach((item, i) => {
      const icon = item.type === "episode" ? "🎙️ " : "🎵";
      const detail =
        item.type === "episode"
          ? `[${item.show}] ${item.name}`
          : `${item.name} — ${item.artist}`;
      console.log(`  ${String(i + 1).padStart(2)}. ${icon} ${detail}`);
    });
    console.log(`\n✅ Dry run complete. ${items.length} items would be added.\n`);
    return;
  }

  // Get the current access token for direct API calls
  const accessToken = spotifyApi.getAccessToken();

  // PUT replaces the entire playlist with up to 100 items at once
  const clearRes = await fetch(`https://api.spotify.com/v1/playlists/${playlistId}/items`, {
    method: "PUT",
    headers: { Authorization: `Bearer ${accessToken}`, "Content-Type": "application/json" },
    body: JSON.stringify({ uris: uris.slice(0, 100) }),
  });
  if (!clearRes.ok) {
    const err = await clearRes.text();
    throw new Error(`Failed to update playlist: ${clearRes.status} ${err}`);
  }

  // If we have more than 100 items, POST the remaining in batches of 100
  for (let i = 100; i < uris.length; i += 100) {
    const batch = uris.slice(i, i + 100);
    const addRes = await fetch(`https://api.spotify.com/v1/playlists/${playlistId}/items`, {
      method: "POST",
      headers: { Authorization: `Bearer ${accessToken}`, "Content-Type": "application/json" },
      body: JSON.stringify({ uris: batch }),
    });
    if (!addRes.ok) {
      const err = await addRes.text();
      throw new Error(`Failed to add batch: ${addRes.status} ${err}`);
    }
  }

  console.log(`\n✅ Playlist updated with ${items.length} items!`);
  console.log(`   🎙️  ${items.filter((i) => i.type === "episode").length} podcast episodes`);
  console.log(`   🎵 ${items.filter((i) => i.type === "track").length} songs\n`);
}

// =============================================================================
// Main — Entry point that orchestrates everything
// =============================================================================

async function main() {
  // Step 1: Load configuration and authentication token
  const config = loadConfig();
  const refreshWindow = resolveRefreshSlot(config);
  const token = loadToken();

  console.log(`\n🚗 Daily Drive — ${refreshWindow.slot} refresh...\n`);
  console.log(
    `🕒 Local time in ${refreshWindow.timeZone}: ${refreshWindow.currentDate} ${refreshWindow.currentTime}`
  );
  console.log(
    `   Morning slot: ${refreshWindow.morningTime} | Evening slot: ${refreshWindow.eveningTime}`
  );

  // Step 2: Create Spotify API client with your app credentials
  const spotifyApi = new SpotifyWebApi({
    clientId: config.spotify.client_id,
    clientSecret: config.spotify.client_secret,
    redirectUri: config.spotify.redirect_uri,
  });

  // Set the tokens so the API client can make authenticated requests
  spotifyApi.setAccessToken(token.access_token);
  spotifyApi.setRefreshToken(token.refresh_token);

  // Step 3: Refresh the access token if it's about to expire
  await refreshTokenIfNeeded(spotifyApi, token);

  // Step 4: Make sure the user has set a real playlist ID
  if (!config.playlist_id || config.playlist_id === "your-playlist-id-here") {
    console.error("❌ Please set your playlist_id in config.yaml");
    process.exit(1);
  }

  // Step 5: Fetch the latest podcast episodes
  const episodes = await fetchPodcastEpisodes(spotifyApi, config.podcasts || []);

  // Step 6: Capture the current episode set for state/debugging
  const currentEpisodeUris = episodes.map((e) => e.uri).sort().join(",");

  // Step 7: Get music tracks
  const effectiveMusicConfig = getEffectiveMusicConfig(config, refreshWindow.slot);
  if (
    refreshWindow.slot === "morning"
    && Array.isArray(config.music?.morning_playlists)
    && config.music.morning_playlists.length > 0
  ) {
    console.log(`🌅 Including ${config.music.morning_playlists.length} morning-only playlist source(s)`);
  }
  const tracks = await fetchAllMusicTracks(spotifyApi, effectiveMusicConfig);

  if (episodes.length === 0 && tracks.length === 0) {
    console.error("❌ No content found! Check your config.yaml settings.");
    process.exit(1);
  }

  // Step 8: Separate pinned episodes (position: "first") from mixable ones
  // Pinned episodes go at the very top of the playlist, before the mix pattern starts
  const pinnedFirst = [];
  const mixableEpisodes = [];
  for (const ep of episodes) {
    if (ep.position === "first") {
      pinnedFirst.push(ep);
    } else {
      mixableEpisodes.push(ep);
    }
  }

  // Step 9: Mix podcasts and music according to the configured pattern
  console.log(`\n🔀 Mixing with pattern: ${config.mix_pattern || "PMMM"}`);
  const mixed = [...pinnedFirst, ...mixContent(mixableEpisodes, tracks, config.mix_pattern)];

  // Step 10: Push the final mixed playlist to Spotify
  await updatePlaylist(spotifyApi, config.playlist_id, mixed);

  // Step 11: Save state so the next run can detect if episodes have changed
  if (!DRY_RUN) {
    const newState = {
      episode_uris: currentEpisodeUris,
      last_updated: new Date().toISOString(),
      refresh_slot: refreshWindow.slot,
    };

    saveState(newState);
    console.log("💾 State saved to state.json");
  }
}

/**
 * Fetches all music tracks (familiar + discovery) based on config.
 * Used by both morning and evening refreshes.
 */
async function fetchAllMusicTracks(spotifyApi, musicConfig) {
  const totalSongs = musicConfig.total_songs || 15;
  const hasGenres = musicConfig.genres && musicConfig.genres.length > 0;

  // When genres are configured, split total_songs 50/50:
  //   - Half "familiar" (your top tracks + source playlists)
  //   - Half "discovery" (genre search results — new music for you)
  const familiarCount = hasGenres ? Math.ceil(totalSongs / 2) : totalSongs;
  const discoveryCount = hasGenres ? totalSongs - familiarCount : 0;

  // Fetch familiar tracks (your top tracks + any source playlists)
  const familiarConfig = { ...musicConfig, total_songs: familiarCount };
  let tracks = await fetchMusicTracks(spotifyApi, familiarConfig);

  // Fetch discovery tracks (genre-based search for new music)
  if (hasGenres && discoveryCount > 0) {
    const genreTracks = await fetchGenreTracks(spotifyApi, musicConfig.genres, discoveryCount);

    // Remove any genre tracks that duplicate songs already in the familiar set
    const familiarUris = new Set(tracks.map((t) => t.uri));
    const newGenreTracks = genreTracks.filter((t) => !familiarUris.has(t.uri));
    tracks = [...tracks, ...newGenreTracks.slice(0, discoveryCount)];
    console.log(`🎵 Music mix: ${familiarCount} familiar + ${newGenreTracks.slice(0, discoveryCount).length} discovery = ${tracks.length} total`);
  }

  return tracks;
}

// Run the main function and handle any uncaught errors
main().catch((err) => {
  console.error("\n❌ Error:", err.message);
  if (err.statusCode === 401) {
    console.error("   Your token may have expired. Run: npm run setup\n");
  }
  process.exit(1);
});
