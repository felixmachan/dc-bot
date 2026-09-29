import logging
import time

import discord
import pytest

import main


class FakeSource(discord.AudioSource):
    """Returns a packet per read; delays reads whose index is in slow_at."""

    def __init__(self, frames, slow_at=()):
        self.frames = frames
        self.slow_at = set(slow_at)
        self.reads = 0
        self.cleaned = 0

    def read(self):
        self.reads += 1
        if self.reads in self.slow_at:
            time.sleep(0.03)
        return b"x" if self.reads <= self.frames else b""

    def is_opus(self):
        return True

    def cleanup(self):
        self.cleaned += 1


class FakeVC:
    channel = None
    latency = 0.01


def pull(source, count, gap=0.02, late_at=()):
    for i in range(1, count + 1):
        source.read()
        time.sleep(0.05 if i in late_at else gap)


@pytest.fixture(autouse=True)
def no_startup_grace(monkeypatch):
    monkeypatch.setattr(main.DiagnosticSource, "STARTUP_GRACE_SEC", 0.0)


def diag_lines(caplog):
    return [r.getMessage() for r in caplog.records if "AUDIO_DIAG" in r.getMessage()]


def test_slow_read_is_reported_as_input_stall(caplog):
    caplog.set_level(logging.INFO, logger="dc_bot")
    inner = FakeSource(10, slow_at={5})
    source = main.DiagnosticSource(inner, 1, FakeVC(), "test")
    pull(source, 10, gap=0.005)
    source.cleanup()

    final = next(line for line in diag_lines(caplog) if " final " in line)
    assert "verdict=input_stall" in final
    assert "slow_reads=1" in final


def test_late_gap_without_slow_read_is_player_starved(caplog):
    caplog.set_level(logging.INFO, logger="dc_bot")
    source = main.DiagnosticSource(FakeSource(10), 1, FakeVC(), "test")
    pull(source, 10, gap=0.02, late_at={4})
    source.cleanup()

    final = next(line for line in diag_lines(caplog) if " final " in line)
    assert "verdict=player_starved" in final
    assert "late_gaps=1" in final


def test_cleanup_runs_once_even_when_called_again():
    inner = FakeSource(3)
    source = main.DiagnosticSource(inner, 1, FakeVC(), "test")
    pull(source, 3, gap=0.0)
    source.cleanup()
    source.cleanup()

    assert inner.cleaned == 1


def test_youtube_opus_stream_is_passed_through_without_reencoding(monkeypatch):
    made = {}

    class FakeOpus(FakeSource):
        def __init__(self, url, **kwargs):
            super().__init__(0)
            made.update(kwargs)

    monkeypatch.setattr(main.discord, "FFmpegOpusAudio", FakeOpus)
    monkeypatch.setattr(main, "AUDIO_DIAGNOSTICS", False)

    main.build_audio_source("u", FakeVC(), {"acodec": "opus", "asr": 48000}, 1)
    assert made["codec"] == "copy"

    main.build_audio_source("u", FakeVC(), {"acodec": "mp4a.40.2", "asr": 44100}, 1)
    assert made["codec"] is None
    assert made["bitrate"] == 128


def test_startup_buffering_is_not_reported_as_a_hitch(caplog, monkeypatch):
    monkeypatch.setattr(main.DiagnosticSource, "STARTUP_GRACE_SEC", 10.0)
    caplog.set_level(logging.INFO, logger="dc_bot")
    source = main.DiagnosticSource(FakeSource(10, slow_at={2}), 1, FakeVC(), "test")
    pull(source, 10, gap=0.001)
    source.cleanup()

    final = next(line for line in diag_lines(caplog) if " final " in line)
    assert "verdict=ok" in final
