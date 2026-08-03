import asyncio

import pytest

import main


class FakeYDL:
    """Stand-in for yt_dlp.YoutubeDL driven by a scripted list of outcomes."""

    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def extract_info(self, target, download=False):
        outcome = FakeYDL.outcomes.pop(0)
        FakeYDL.calls.append(target)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


@pytest.fixture(autouse=True)
def reset_fake_ydl(monkeypatch):
    FakeYDL.outcomes = []
    FakeYDL.calls = []
    monkeypatch.setattr(main.yt_dlp, "YoutubeDL", FakeYDL)


def flat_entry(video_id, title, **extra):
    entry = {"id": video_id, "title": title}
    entry.update(extra)
    return entry


def test_search_drops_premieres_and_ranks_live_streams_last():
    FakeYDL.outcomes = [{
        "entries": [
            flat_entry("aaa", "Live stream", is_live=True),
            flat_entry("bbb", "Upcoming premiere", live_status="is_upcoming"),
            flat_entry("ddd", "Still processing", live_status="post_live"),
            flat_entry("ccc", "Good song"),
        ]
    }]

    candidates = asyncio.run(main.search_youtube("valami"))

    # Premiere and post_live are unplayable and dropped; the live stream survives
    # but must not outrank a real upload.
    assert candidates == [
        ("https://www.youtube.com/watch?v=ccc", "Good song"),
        ("https://www.youtube.com/watch?v=aaa", "Live stream"),
    ]
    assert FakeYDL.calls == [f"ytsearch{main.SEARCH_CANDIDATES}:valami"]


def test_search_keeps_live_stream_when_it_is_the_only_option():
    FakeYDL.outcomes = [{"entries": [flat_entry("aaa", "lofi radio", is_live=True)]}]

    assert asyncio.run(main.search_youtube("lofi hip hop radio")) == [
        ("https://www.youtube.com/watch?v=aaa", "lofi radio")
    ]


def test_search_returns_empty_list_on_extractor_error():
    FakeYDL.outcomes = [RuntimeError("yt-dlp exploded")]

    assert asyncio.run(main.search_youtube("valami")) == []


def test_search_short_circuits_for_urls():
    url = "https://www.youtube.com/watch?v=ccc"

    assert asyncio.run(main.search_youtube(url)) == [(url, url)]
    assert FakeYDL.calls == []


def test_resolve_falls_through_to_next_candidate():
    FakeYDL.outcomes = [
        RuntimeError("Video unavailable"),
        {"title": "Working song", "url": "https://media.example/audio"},
    ]

    resolved = asyncio.run(main.resolve_stream_url([
        ("https://www.youtube.com/watch?v=dead", "dead"),
        ("https://www.youtube.com/watch?v=live", "live"),
    ]))

    assert resolved == ("https://media.example/audio", "Working song")


def test_resolve_returns_none_when_every_candidate_fails():
    FakeYDL.outcomes = [RuntimeError("nope"), RuntimeError("nope")]

    resolved = asyncio.run(main.resolve_stream_url([
        ("https://www.youtube.com/watch?v=a", "a"),
        ("https://www.youtube.com/watch?v=b", "b"),
    ]))

    assert resolved is None


def test_resolve_stops_at_candidate_cap():
    FakeYDL.outcomes = [RuntimeError("nope")] * 5
    candidates = [(f"https://www.youtube.com/watch?v={i}", str(i)) for i in range(5)]

    assert asyncio.run(main.resolve_stream_url(candidates)) is None
    assert len(FakeYDL.calls) == main.MAX_RESOLVE_CANDIDATES


def test_resolve_picks_best_audio_only_format_when_url_missing():
    FakeYDL.outcomes = [{
        "title": "Formats only",
        "formats": [
            {"acodec": "opus", "vcodec": "none", "url": "low", "abr": 64},
            {"acodec": "opus", "vcodec": "none", "url": "high", "abr": 160},
            {"acodec": "opus", "vcodec": "avc1", "url": "video", "abr": 320},
            {"acodec": "opus", "vcodec": "none", "url": "hls", "abr": 999, "protocol": "m3u8"},
        ],
    }]

    resolved = asyncio.run(main.resolve_stream_url([("https://www.youtube.com/watch?v=a", "a")]))

    assert resolved == ("high", "Formats only")


def test_resolve_accepts_live_stream():
    FakeYDL.outcomes = [{"title": "lofi radio", "url": "stream", "is_live": True}]

    resolved = main.resolve_stream_url([("https://www.youtube.com/watch?v=a", "a")])

    assert asyncio.run(resolved) == ("stream", "lofi radio")


def test_resolve_rejects_unstarted_premiere():
    FakeYDL.outcomes = [{"title": "Premiere", "url": "x", "live_status": "is_upcoming"}]

    assert asyncio.run(main.resolve_stream_url([("https://www.youtube.com/watch?v=a", "a")])) is None


def test_resolve_track_searches_and_caches_candidates_for_plain_terms():
    FakeYDL.outcomes = [
        {"entries": [flat_entry("ccc", "Found song")]},
        {"title": "Found song", "url": "https://media.example/audio"},
    ]
    track = main.QueuedTrack(source="artist - song", title="artist - song", target=None)

    resolved = asyncio.run(main.resolve_track(track))

    assert resolved == ("https://media.example/audio", "Found song")
    assert track.candidates == [("https://www.youtube.com/watch?v=ccc", "Found song")]


def test_resolve_track_reuses_cached_candidates_without_researching():
    FakeYDL.outcomes = [{"title": "Cached", "url": "https://media.example/audio"}]
    track = main.QueuedTrack(
        source="artist - song",
        title="artist - song",
        target=None,
        candidates=[("https://www.youtube.com/watch?v=ccc", "Cached")],
    )

    assert asyncio.run(main.resolve_track(track)) == ("https://media.example/audio", "Cached")
    # Only the extraction call; no fresh ytsearch.
    assert FakeYDL.calls == ["https://www.youtube.com/watch?v=ccc"]
