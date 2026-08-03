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


def test_player_panel_falls_back_to_plain_text_when_the_embed_cannot_be_sent(monkeypatch):
    """Without the Embed Links permission the announcement must still get through."""
    guild = FakeGuild(705)
    calls = []

    async def refusing_safe_send(target, message=None, *, embed=None, view=None):
        calls.append({"message": message, "embed": embed})
        return None if embed is not None else "sent"

    monkeypatch.setattr(main, "safe_send", refusing_safe_send)

    track = main.QueuedTrack(source="x", title="Valami szám", target=None)
    asyncio.run(main.send_player_message(guild, track))

    assert calls[0]["embed"] is not None, "the embed panel is attempted first"
    assert len(calls) == 2, "a plain text fallback must follow the failed embed"
    assert "Valami szám" in calls[1]["message"]
    assert guild.id not in main.player_messages


def test_player_panel_is_remembered_when_it_sends(monkeypatch):
    guild = FakeGuild(706)

    async def ok_safe_send(target, message=None, *, embed=None, view=None):
        return "the-message"

    monkeypatch.setattr(main, "safe_send", ok_safe_send)

    asyncio.run(main.send_player_message(guild, main.QueuedTrack(source="x", title="t", target=None)))

    assert main.player_messages[guild.id] == "the-message"


def test_clear_queue_empties_it():
    queue = main.get_guild_queue(704)
    for i in range(3):
        queue.put_nowait(main.QueuedTrack(source=str(i), title=str(i), target=None))

    main.clear_queue(704)

    assert queue.empty()


class FakeResponse:
    def __init__(self):
        self.edited = None
        self.sent = None

    async def edit_message(self, **kwargs):
        self.edited = kwargs

    async def send_message(self, content=None, **kwargs):
        self.sent = {"content": content, **kwargs}


class FakeInteraction:
    def __init__(self, guild, user_id):
        self.guild = guild
        self.user = SimpleNamespace(id=user_id, display_name="tester")
        self.response = FakeResponse()


def chooser_results():
    return [
        main.SearchResult(url="https://yt/1", title="Rossz találat"),
        main.SearchResult(url="https://yt/2", title="Jó találat"),
        main.SearchResult(url="https://yt/3", title="Harmadik"),
    ]


def test_chooser_has_one_button_per_result_plus_cancel():
    view = main.TrackChooserView("q", chooser_results(), requester_id=1, target=None)

    assert len(view.children) == 4
    assert view.children[-1].custom_id == "chooser:cancel"
    assert view.timeout == main.CHOOSER_TIMEOUT_SEC


def test_choosing_queues_that_result_and_keeps_the_others_as_fallbacks(monkeypatch):
    guild = FakeGuild(710)
    guild.voice_client = None          # nothing playing, so no play_next call
    results = chooser_results()
    view = main.TrackChooserView("q", results, requester_id=1, target="orig-target")
    interaction = FakeInteraction(guild, user_id=1)

    # children[1] is the second result's button.
    asyncio.run(view.children[1].callback(interaction))

    queue = main.get_guild_queue(guild.id)
    assert queue.qsize() == 1
    track = queue.get_nowait()
    assert track.title == "Jó találat"
    assert track.source == "https://yt/2"
    assert track.target == "orig-target"
    # The chosen hit leads, the rest stay as fallbacks for a dead video.
    assert [c.title for c in track.candidates] == ["Jó találat", "Rossz találat", "Harmadik"]
    assert "Jó találat" in interaction.response.edited["content"]
    assert interaction.response.edited["view"] is None


def test_only_the_requester_may_choose():
    guild = FakeGuild(711)
    view = main.TrackChooserView("q", chooser_results(), requester_id=1, target=None)
    interaction = FakeInteraction(guild, user_id=999)

    asyncio.run(view.children[0].callback(interaction))

    assert main.get_guild_queue(guild.id).empty(), "a stranger must not queue anything"
    assert "nem te indítottad" in interaction.response.sent["content"]
    assert interaction.response.sent["ephemeral"] is True


def test_cancel_clears_the_prompt_without_queueing():
    guild = FakeGuild(712)
    view = main.TrackChooserView("q", chooser_results(), requester_id=1, target=None)
    interaction = FakeInteraction(guild, user_id=1)

    asyncio.run(view.children[-1].callback(interaction))

    assert main.get_guild_queue(guild.id).empty()
    assert "Megszakítva" in interaction.response.edited["content"]


def test_chooser_embed_lists_every_result_with_length_and_channel():
    results = [
        main.SearchResult(url="https://yt/1", title="Első", duration=222, uploader="T. Danny"),
        main.SearchResult(url="https://yt/2", title="Élő adás", is_live=True),
    ]

    embed = main.build_chooser_embed("tdanny szivtipro", results)

    assert len(embed.fields) == 2
    assert "Első" in embed.fields[0].name
    assert "3:42" in embed.fields[0].value and "T. Danny" in embed.fields[0].value
    assert "élő" in embed.fields[1].value
