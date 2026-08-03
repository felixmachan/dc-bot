#!/usr/bin/env python3
"""One-time Spotify user authorisation for the bot.

App-only credentials cannot read playlist contents; Spotify answers 403. This
walks through the Authorization Code flow once and writes a refresh token that
the bot reuses from then on.

Before running, on https://developer.spotify.com/dashboard for this app:

  1. Settings -> Redirect URIs: add exactly the value of SPOTIFY_REDIRECT_URI
     (http://127.0.0.1:8888/callback is a fine choice), then Save.
  2. Settings -> User Management: add the Spotify account you want the bot to
     read playlists as. A development-mode app allows up to 25 such accounts.
  3. Put SPOTIFY_REDIRECT_URI in .env next to the client id and secret.

Then:  python tools/spotify_authorize.py

The browser step needs a machine with a browser. If the bot runs on a headless
server, run this locally and copy the generated token file over, next to main.py.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import main


def fail(message: str) -> None:
    sys.exit(f"\n❌ {message}")


def run() -> None:
    if not main.spotipy:
        fail("A spotipy nincs telepitve: pip install -r requirements.txt")
    if not main.SPOTIFY_CLIENT_ID or not main.SPOTIFY_CLIENT_SECRET:
        fail("Hianyzik a SPOTIFY_CLIENT_ID vagy a SPOTIFY_CLIENT_SECRET a .env-bol.")
    if not main.SPOTIFY_REDIRECT_URI:
        fail(
            "Nincs SPOTIFY_REDIRECT_URI a .env-ben.\n"
            "   Tegyel be egyet, pl.: SPOTIFY_REDIRECT_URI=http://127.0.0.1:8888/callback\n"
            "   es ugyanezt vedd fel a Spotify dashboardon is (Settings -> Redirect URIs)."
        )

    oauth = main.build_spotify_oauth()
    if not oauth:
        fail("Nem sikerult felepiteni a Spotify OAuth managert.")

    print(f"Token fajl: {main.SPOTIFY_TOKEN_CACHE}")
    print(f"Redirect URI: {main.SPOTIFY_REDIRECT_URI}")
    print(f"Jogosultsagok: {main.SPOTIFY_SCOPE}\n")

    print("1) Nyisd meg ezt a linket es engedelyezd a hozzaferest:\n")
    print("   " + oauth.get_authorize_url() + "\n")
    print("2) A bongeszo atiranyit egy olyan cimre, ami valoszinuleg nem tolt be.")
    print("   Ez rendben van - a cimsorbol masold ki a TELJES URL-t.\n")

    redirected = input("Beillesztett URL: ").strip()
    if not redirected:
        fail("Nem adtal meg URL-t.")

    try:
        code = oauth.parse_response_code(redirected)
        token = oauth.get_access_token(code, as_dict=True, check_cache=False)
    except Exception as exc:
        fail(f"A token keres nem sikerult: {exc}")

    if not token:
        fail("A Spotify nem adott vissza tokent.")

    client = main.spotipy.Spotify(auth_manager=oauth)
    me = client.current_user()
    print(f"\n✅ Sikeres bejelentkezes: {me.get('display_name')} ({me.get('id')})")
    print(f"✅ Token elmentve ide: {main.SPOTIFY_TOKEN_CACHE}")
    print("   Ha a bot mas gepen fut, masold at ezt a fajlt a main.py melle, majd inditsd ujra.")

    print("\nGyors proba egy sajat playlisten...")
    try:
        playlists = client.current_user_playlists(limit=1)
        items = playlists.get("items") or []
        if not items:
            print("   Nincs sajat lejatszasi listad, kihagyva.")
            return
        first = items[0]
        tracks = client.playlist_items(first["id"], limit=3)
        print(f"   ✅ '{first['name']}' olvashato ({len(tracks.get('items', []))} szam mintaban)")
        print("   A playlist tamogatas mukodni fog.")
    except Exception as exc:
        status = getattr(exc, "http_status", "?")
        print(f"   ⚠️  A playlist olvasas igy is elszallt (HTTP {status}).")
        print("   Ilyenkor a Spotify az appnak egyaltalan nem engedi a listakat;")
        print("   marad a szam- es album-link, illetve a YouTube playlist.")


if __name__ == "__main__":
    run()
