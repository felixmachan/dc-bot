import asyncio
from types import SimpleNamespace

import pytest

import main


class FakeVoiceClient:
    def __init__(self):
        self.disconnect_called = False

    def is_connected(self):
        return True

    def is_playing(self):
        return False

    def is_paused(self):
        return False

    async def disconnect(self, force=False):
        self.disconnect_called = True


class FakeGuild:
    def __init__(self, guild_id):
        self.id = guild_id
        self.voice_client = FakeVoiceClient()


@pytest.fixture(autouse=True)
def isolated_state(monkeypatch):
    """Give each test a clean queue/lock namespace and a harmless bot loop."""
    monkeypatch.setattr(main, "song_queue", {})
    monkeypatch.setattr(main, "playback_locks", {})
    monkeypatch.setattr(main, "current_track", {})
    monkeypatch.setattr(main, "now_playing", {})
    monkeypatch.setattr(main, "player_messages", {})

    scheduled = []

    def fake_create_task(coro):
        # Close the coroutine so the test does not leak an un-awaited task.
        coro.close()
        scheduled.append(coro)
        return None

    monkeypatch.setattr(main, "bot", SimpleNamespace(loop=SimpleNamespace(create_task=fake_create_task)))

    async def fake_ensure(guild, target=None):
        return guild.voice_client

    monkeypatch.setattr(main, "ensure_voice_connection", fake_ensure)
    return scheduled


def test_failing_track_is_requeued_until_the_attempt_cap(monkeypatch):
    guild = FakeGuild(700)
    sent = []

    async def failing_start(g, track, announce=True):
        return False

    async def fake_safe_send(target, message=None, **kwargs):
        sent.append(message)
        return None

    monkeypatch.setattr(main, "start_track", failing_start)
    monkeypatch.setattr(main, "safe_send", fake_safe_send)

    queue = main.get_guild_queue(guild.id)
    track = main.QueuedTrack(source="broken", title="Broken song", target=None)
    queue.put_nowait(track)

    # Each pass re-queues the track until the cap is reached.
    for expected in range(1, main.MAX_TRACK_START_ATTEMPTS):
        asyncio.run(main.play_next(guild))
        assert track.attempts == expected
        assert queue.qsize() == 1, "track should still be queued below the cap"
        assert sent == []

    # The final failure drops it instead of looping forever.
    asyncio.run(main.play_next(guild))
    assert track.attempts == main.MAX_TRACK_START_ATTEMPTS
    assert queue.empty(), "track must be dropped once the cap is hit"
    assert sent and "Broken song" in sent[0]


def test_successful_track_is_not_requeued(monkeypatch):
    guild = FakeGuild(701)

    async def ok_start(g, track, announce=True):
        return True

    monkeypatch.setattr(main, "start_track", ok_start)

    queue = main.get_guild_queue(guild.id)
    queue.put_nowait(main.QueuedTrack(source="fine", title="Fine song", target=None))

    asyncio.run(main.play_next(guild))

    assert queue.empty()


def test_empty_queue_disconnects_and_clears_state(monkeypatch):
    guild = FakeGuild(702)
    main.current_track[guild.id] = main.QueuedTrack(source="x", title="x", target=None)
    main.now_playing[guild.id] = "x"

    asyncio.run(main.play_next(guild))

    assert guild.voice_client.disconnect_called
    assert guild.id not in main.current_track
    assert guild.id not in main.now_playing


def test_queued_titles_does_not_consume_the_queue():
    queue = main.get_guild_queue(703)
    queue.put_nowait(main.QueuedTrack(source="a", title="Első", target=None))
    queue.put_nowait(main.QueuedTrack(source="b", title="Második", target=None))

    assert main.queued_titles(703) == ["Első", "Második"]
    assert queue.qsize() == 2


def test_clear_queue_empties_it():
    queue = main.get_guild_queue(704)
    for i in range(3):
        queue.put_nowait(main.QueuedTrack(source=str(i), title=str(i), target=None))

    main.clear_queue(704)

    assert queue.empty()
