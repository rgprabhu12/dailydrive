#!/usr/bin/env python3
import os
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from daily_drive_common import DailyDriveError, SpotifyClient, ensure_spotify_credentials, load_config, save_token

TOKEN_FILE = Path(".spotify-token.json")
SCOPES = [
    "playlist-modify-public",
    "playlist-modify-private",
    "playlist-read-private",
    "playlist-read-collaborative",
    "user-library-read",
    "user-read-private",
    "user-read-recently-played",
    "user-top-read",
]


def main() -> int:
    if TOKEN_FILE.exists():
        TOKEN_FILE.unlink()
        print("🗑️  Deleted old token — starting fresh auth")

    config = load_config()
    client_id, client_secret, redirect_uri = ensure_spotify_credentials(config)
    spotify = SpotifyClient(client_id, client_secret, redirect_uri)
    parsed = urlparse(redirect_uri)
    port = parsed.port or 8888
    done = threading.Event()
    result: dict[str, str] = {}

    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            query = parse_qs(urlparse(self.path).query)
            code = query.get("code", [None])[0]
            error = query.get("error", [None])[0]

            if error:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(f"<h1>Error: {error}</h1><p>Please try again.</p>".encode("utf-8"))
                result["error"] = error
                done.set()
                return

            if not code:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"<h1>Error</h1><p>Missing authorization code.</p>")
                result["error"] = "Missing authorization code"
                done.set()
                return

            try:
                data = spotify.authorization_code_grant(code)
                token_data = {
                    "access_token": data["access_token"],
                    "refresh_token": data["refresh_token"],
                    "expires_at": int(__import__("time").time() * 1000) + int(data["expires_in"]) * 1000,
                }
                save_token(token_data)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(
                    b"<h1>\xe2\x9c\x85 Success!</h1><p>You can close this window. Daily Drive is ready to go!</p>"
                )
                result["ok"] = "1"
            except Exception as exc:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(f"<h1>Error</h1><p>{exc}</p>".encode("utf-8"))
                result["error"] = str(exc)
            finally:
                done.set()

        def log_message(self, format, *args):
            return

    server = HTTPServer(("127.0.0.1", port), CallbackHandler)
    auth_url = spotify.create_authorize_url(SCOPES, "dailydrive")

    print("\n🎵 Daily Drive — Setup\n")
    print("Open this URL in your browser to authorize:\n")
    print(f"  {auth_url}\n")

    try:
        webbrowser.open(auth_url)
    except Exception:
        pass

    print(f"(If needed, make sure your Spotify app redirect URI is set to: {redirect_uri})\n")

    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    done.wait()
    server.server_close()

    if result.get("error"):
        raise DailyDriveError(f"Authorization failed: {result['error']}")

    print("\n✅ Authentication successful!")
    print(f"   Token saved to {TOKEN_FILE}")
    print("\n   You can now run: python3 daily_drive.py\n")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except DailyDriveError as exc:
        print(f"\n❌ {exc}\n")
        raise SystemExit(1)
