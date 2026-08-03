import asyncio

import pytest

import main
from test_search import FakeYDL, flat_entry  # noqa: F401  (fixtures live there)


@pytest.fixture(autouse=True)
def reset_fake_ydl(monkeypatch):
    FakeYDL.outcomes = []
    FakeYDL.calls = []
    monkeypatch.setattr(main.yt_dlp, "YoutubeDL", FakeYDL)


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://www.youtube.com/playlist?list=PLabc", True),
        ("https://music.youtube.com/playlist?list=PLabc", True),
        # A video that merely sits in a playlist stays one video.
        ("https://www.youtube.com/watch?v=abc&list=PLabc", False),
        ("https://www.youtube.com/watch?v=abc", False),
        ("https://youtu.be/abc", False),
        ("https://open.spotify.com/playlist/abc", False),
        ("csak egy kereses", False),
    ],
)
def test_playlist_url_detection(url, expected):
    assert main.is_youtube_playlist_url(url) is expected


def test_playlist_expands_to_every_entry():
    FakeYDL.outcomes = [{
        "entries": [
            flat_entry("a", "Első szám", duration=100, uploader="Csatorna"),
            flat_entry("b", "Második szám", duration=200),
        ]
    }]

    entries, error = asyncio.run(
        main.get_youtube_playlist("https://www.youtube.com/playlist?list=PLabc")
    )

    assert error is None
    assert [e.title for e in entries] == ["Első szám", "Második szám"]
    assert entries[0].url == "https://www.youtube.com/watch?v=a"
    assert entries[0].duration == 100
    assert entries[0].uploader == "Csatorna"


def test_playlist_extraction_asks_yt_dlp_for_the_whole_list():
    FakeYDL.outcomes = [{"entries": [flat_entry("a", "x")]}]

    asyncio.run(main.get_youtube_playlist("https://www.youtube.com/playlist?list=PLabc"))

    # noplaylist must be off here, unlike every other extraction in the bot.
    opts = FakeYDL.last_opts
    assert opts["noplaylist"] is False
    assert opts["playlistend"] == main.YOUTUBE_PLAYLIST_LIMIT


def test_playlist_skips_unplayable_entries():
    FakeYDL.outcomes = [{
        "entries": [
            flat_entry("a", "Premier", live_status="is_upcoming"),
            flat_entry("b", "Rendes szám"),
        ]
    }]

    entries, error = asyncio.run(
        main.get_youtube_playlist("https://www.youtube.com/playlist?list=PLabc")
    )

    assert error is None
    assert [e.title for e in entries] == ["Rendes szám"]


def test_empty_playlist_is_reported():
    FakeYDL.outcomes = [{"entries": []}]

    assert asyncio.run(
        main.get_youtube_playlist("https://www.youtube.com/playlist?list=PLabc")
    ) == ([], "empty")


def test_playlist_failure_is_reported():
    FakeYDL.outcomes = [RuntimeError("private playlist")]

    entries, error = asyncio.run(
        main.get_youtube_playlist("https://www.youtube.com/playlist?list=PLabc")
    )

    assert (entries, error) == ([], "error")
    assert error in main.YOUTUBE_PLAYLIST_ERRORS


def test_every_playlist_error_code_has_a_message():
    for code in ["timeout", "empty", "error"]:
        assert main.YOUTUBE_PLAYLIST_ERRORS[code].startswith("❌")
