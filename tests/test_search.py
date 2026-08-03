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


def result(video_id, title=None):
    """A SearchResult standing in for one search hit."""
    return main.SearchResult(
        url=f"https://www.youtube.com/watch?v={video_id}", title=title or video_id
    )


def flat_entry(video_id, title, **extra):
    entry = {"id": video_id, "title": title}
    entry.update(extra)
    return entry


def urls(results):
    return [r.url for r in results]


def test_search_drops_premieres_and_ranks_live_streams_last():
    FakeYDL.outcomes = [{
        "entries": [
            flat_entry("aaa", "Live stream", is_live=True),
            flat_entry("bbb", "Upcoming premiere", live_status="is_upcoming"),
            flat_entry("ddd", "Still processing", live_status="post_live"),
            flat_entry("ccc", "Good song"),
        ]
    }]

    results = asyncio.run(main.search_youtube("valami"))

    # Premiere and post_live are unplayable and dropped; the live stream survives
    # but must not outrank a real upload.
    assert urls(results) == [
        "https://www.youtube.com/watch?v=ccc",
        "https://www.youtube.com/watch?v=aaa",
    ]
    assert FakeYDL.calls == [f"ytsearch{main.SEARCH_CANDIDATES}:valami"]


def test_search_keeps_live_stream_when_it_is_the_only_option():
    FakeYDL.outcomes = [{"entries": [flat_entry("aaa", "lofi radio", is_live=True)]}]

    results = asyncio.run(main.search_youtube("lofi hip hop radio"))

    assert urls(results) == ["https://www.youtube.com/watch?v=aaa"]


def test_search_promotes_the_title_that_matches_the_query():
    """The real 'tdanny szivtipro' case: YouTube ranked the wanted song fourth."""
    FakeYDL.outcomes = [{
        "entries": [
            flat_entry("v1", "T. Danny - Van Valami feat. RZMVS (Official Music Video)"),
            flat_entry("v2", "T. Danny - xXx (Official Music Video)"),
            flat_entry("v3", "T. Danny - VIDÉKI CSAJSZI (Official Music Video)"),
            flat_entry("v4", "T. Danny - SZÍVTIPRÓ (Official Music Video)"),
            flat_entry("v5", "T. Danny - SZÖRNYETEG (Official Visualizer)"),
        ]
    }]

    results = asyncio.run(main.search_youtube("tdanny szivtipro"))

    assert results[0].url == "https://www.youtube.com/watch?v=v4"
    assert results[0].score == 1.0
    assert results[0].score >= main.SEARCH_CONFIDENT_SCORE, "should play without asking"


def test_search_keeps_youtube_order_when_scores_tie():
    FakeYDL.outcomes = [{
        "entries": [
            flat_entry("v1", "Radiohead - Creep"),
            flat_entry("v2", "Radiohead - Creep (live)"),
        ]
    }]

    results = asyncio.run(main.search_youtube("radiohead creep"))

    assert urls(results) == [
        "https://www.youtube.com/watch?v=v1",
        "https://www.youtube.com/watch?v=v2",
    ]


def test_search_reports_low_confidence_when_the_query_is_not_in_any_title():
    FakeYDL.outcomes = [{
        "entries": [
            flat_entry("v1", "Valami teljesen mas"),
            flat_entry("v2", "Megint mas"),
        ]
    }]

    results = asyncio.run(main.search_youtube("tdanny szivtipro"))

    assert results[0].score < main.SEARCH_CONFIDENT_SCORE, "should ask the user"


def test_search_captures_duration_and_uploader_for_the_picker():
    FakeYDL.outcomes = [{
        "entries": [flat_entry("v1", "Egy szám", duration=222, uploader="T. Danny")]
    }]

    result = asyncio.run(main.search_youtube("egy szam"))[0]

    assert result.duration == 222
    assert result.uploader == "T. Danny"


def test_match_score_ignores_accents_and_run_together_words():
    assert main.match_score("tdanny szivtipro", "T. Danny - SZÍVTIPRÓ (Official)") == 1.0
    assert main.match_score("tdanny szivtipro", "T. Danny - Van Valami") == 0.5
    assert main.match_score("valami", "Teljesen mas cim") == 0.0
    assert main.match_score("", "Barmi") == 0.0


def test_format_duration():
    assert main.format_duration(222) == "3:42"
    assert main.format_duration(59) == "0:59"
    assert main.format_duration(3725) == "1:02:05"
    assert main.format_duration(None) == "?"


def test_search_returns_empty_list_on_extractor_error():
    FakeYDL.outcomes = [RuntimeError("yt-dlp exploded")]

    assert asyncio.run(main.search_youtube("valami")) == []


def test_search_short_circuits_for_urls():
    url = "https://www.youtube.com/watch?v=ccc"

    results = asyncio.run(main.search_youtube(url))

    assert [(r.url, r.title) for r in results] == [(url, url)]
    assert results[0].score >= main.SEARCH_CONFIDENT_SCORE, "a URL is never ambiguous"
    assert FakeYDL.calls == []


def test_resolve_falls_through_to_next_candidate():
    FakeYDL.outcomes = [
        RuntimeError("Video unavailable"),
        {"title": "Working song", "url": "https://media.example/audio"},
    ]

    resolved = asyncio.run(main.resolve_stream_url([
        result("dead"),
        result("live"),
    ]))

    assert resolved == ("https://media.example/audio", "Working song")


def test_resolve_returns_none_when_every_candidate_fails():
    FakeYDL.outcomes = [RuntimeError("nope"), RuntimeError("nope")]

    resolved = asyncio.run(main.resolve_stream_url([result("a"), result("b")]))

    assert resolved is None


def test_resolve_stops_at_candidate_cap():
    FakeYDL.outcomes = [RuntimeError("nope")] * 5
    candidates = [result(str(i)) for i in range(5)]

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

    resolved = asyncio.run(main.resolve_stream_url([result("a")]))

    assert resolved == ("high", "Formats only")


def test_resolve_accepts_live_stream():
    FakeYDL.outcomes = [{"title": "lofi radio", "url": "stream", "is_live": True}]

    resolved = main.resolve_stream_url([result("a")])

    assert asyncio.run(resolved) == ("stream", "lofi radio")


def test_resolve_rejects_unstarted_premiere():
    FakeYDL.outcomes = [{"title": "Premiere", "url": "x", "live_status": "is_upcoming"}]

    assert asyncio.run(main.resolve_stream_url([result("a")])) is None


def test_resolve_track_searches_and_caches_candidates_for_plain_terms():
    FakeYDL.outcomes = [
        {"entries": [flat_entry("ccc", "Found song")]},
        {"title": "Found song", "url": "https://media.example/audio"},
    ]
    track = main.QueuedTrack(source="artist - song", title="artist - song", target=None)

    resolved = asyncio.run(main.resolve_track(track))

    assert resolved == ("https://media.example/audio", "Found song")
    assert [(c.url, c.title) for c in track.candidates] == [
        ("https://www.youtube.com/watch?v=ccc", "Found song")
    ]


def test_resolve_track_reuses_cached_candidates_without_researching():
    FakeYDL.outcomes = [{"title": "Cached", "url": "https://media.example/audio"}]
    track = main.QueuedTrack(
        source="artist - song",
        title="artist - song",
        target=None,
        candidates=[result("ccc", "Cached")],
    )

    assert asyncio.run(main.resolve_track(track)) == ("https://media.example/audio", "Cached")
    # Only the extraction call; no fresh ytsearch.
    assert FakeYDL.calls == ["https://www.youtube.com/watch?v=ccc"]


def test_info_embed_covers_every_command_and_button():
    embed = main.build_info_embed()
    blob = embed.description + " ".join(f"{f.name} {f.value}" for f in embed.fields)

    for command in ["play", "skip", "pause", "resume", "queue", "shuffle", "stop", "join", "leave", "np"]:
        assert f"/music {command}" in blob or f"`{main.PREFIX}{command}" in blob, command
    for button in ["Pause/Resume", "Skip", "Stop", "Shuffle", "Queue"]:
        assert button in blob, button
    # The picker is explained too.
    assert "1️⃣" in blob and "Mégse" in blob
    # Discord's own limits.
    assert len(embed) <= 6000
    assert all(len(f.value) <= 1024 and len(f.name) <= 256 for f in embed.fields)
