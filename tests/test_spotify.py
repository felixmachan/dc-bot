import asyncio
from types import SimpleNamespace

import pytest

import main


class SpotifyError(Exception):
    def __init__(self, status):
        super().__init__(f"HTTP {status}")
        self.http_status = status


class FakeSpotify:
    """Minimal spotipy stand-in with scripted responses and pagination."""

    def __init__(self, **responses):
        self.responses = responses
        self.next_pages = responses.get("_pages", {})

    def _maybe_raise(self, key):
        value = self.responses.get(key)
        if isinstance(value, BaseException):
            raise value
        return value

    def track(self, _id):
        return self._maybe_raise("track")

    def album_tracks(self, _id, limit=50):
        return self._maybe_raise("album_tracks")

    def playlist_items(self, _id, **kwargs):
        return self._maybe_raise("playlist_items")

    def next(self, page):
        return self.next_pages.get(id(page)) or self.responses.get("next_page")


def track_obj(name, artist):
    return {"name": name, "artists": [{"name": artist}]}


@pytest.fixture
def spotify(monkeypatch):
    def install(**responses):
        client = FakeSpotify(**responses)
        monkeypatch.setattr(main, "SPOTIFY_CLIENT", client)
        return client
    return install


def test_playlist_403_reports_forbidden_not_empty(spotify):
    """The old code called this 'empty or unreadable', which was misleading."""
    spotify(playlist_items=SpotifyError(403))

    tracks, error = asyncio.run(
        main.get_spotify_tracks("https://open.spotify.com/playlist/0N2jDY7XM9v4YrhzDtGkqi")
    )

    assert tracks == []
    assert error == "forbidden"
    assert "Album" in main.spotify_error_message(error)


def test_algorithmic_playlist_404_reports_not_found(spotify):
    spotify(playlist_items=SpotifyError(404))

    tracks, error = asyncio.run(
        main.get_spotify_tracks("https://open.spotify.com/playlist/37i9dQZF1E8OvkJP7Keg2G?si=abc")
    )

    assert (tracks, error) == ([], "not_found")
    assert "Daily Mix" in main.spotify_error_message(error)


def test_track_url_resolves_to_a_search_term(spotify):
    spotify(track=track_obj("Never Gonna Give You Up", "Rick Astley"))

    tracks, error = asyncio.run(
        main.get_spotify_tracks("https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT")
    )

    assert tracks == ["Never Gonna Give You Up Rick Astley"]
    assert error is None


def test_album_follows_pagination(spotify):
    first = {"items": [track_obj("A", "X"), track_obj("B", "X")], "next": "url"}
    second = {"items": [track_obj("C", "X")], "next": None}
    spotify(album_tracks=first, next_page=second)

    tracks, error = asyncio.run(
        main.get_spotify_tracks("https://open.spotify.com/album/5Z9iiGl2FcIfa3BMiv6OIw")
    )

    assert tracks == ["A X", "B X", "C X"], "a second page must not be dropped"
    assert error is None


def test_track_cap_is_enforced(spotify):
    page = {"items": [track_obj(str(i), "X") for i in range(200)], "next": None}
    spotify(album_tracks=page)

    tracks, _ = asyncio.run(
        main.get_spotify_tracks("https://open.spotify.com/album/5Z9iiGl2FcIfa3BMiv6OIw")
    )

    assert len(tracks) == main.MAX_SPOTIFY_TRACKS


def test_empty_result_is_reported_as_empty(spotify):
    spotify(album_tracks={"items": [], "next": None})

    assert asyncio.run(
        main.get_spotify_tracks("https://open.spotify.com/album/5Z9iiGl2FcIfa3BMiv6OIw")
    ) == ([], "empty")


def test_unparseable_url(spotify):
    spotify()

    assert asyncio.run(main.get_spotify_tracks("https://open.spotify.com/artist/abc")) == ([], "bad_url")


def test_missing_client(monkeypatch):
    monkeypatch.setattr(main, "SPOTIFY_CLIENT", None)

    assert asyncio.run(
        main.get_spotify_tracks("https://open.spotify.com/track/abc")
    ) == ([], "no_client")


def test_every_error_code_has_a_message():
    for code in ["no_client", "bad_url", "forbidden", "not_found", "empty", "error"]:
        assert main.spotify_error_message(code).startswith("❌")
    assert main.spotify_error_message("valami_ismeretlen") == main.SPOTIFY_ERROR_MESSAGES["error"]
