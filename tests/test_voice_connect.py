import asyncio
from types import SimpleNamespace

import pytest

import main


class FakeVoiceClient:
    def __init__(self, channel=None, connected=True):
        self.channel = channel
        self._connected = connected
        self.disconnect_called = False

    def is_connected(self):
        return self._connected

    async def disconnect(self, force=False):
        self.disconnect_called = True
        self._connected = False

    async def move_to(self, channel):
        self.channel = channel
        self._connected = True


class FakeChannel:
    def __init__(self, guild, channel_id=99, outcomes=None):
        self.guild = guild
        self.id = channel_id
        self._outcomes = list(outcomes or ["success"])

    async def connect(self, timeout, reconnect, self_deaf):
        if self._outcomes:
            outcome = self._outcomes.pop(0)
        else:
            outcome = "success"
        if isinstance(outcome, BaseException):
            raise outcome
        self.guild.voice_client = FakeVoiceClient(channel=self, connected=True)
        return self.guild.voice_client


class FakeGuild:
    def __init__(self, guild_id=1, voice_client=None):
        self.id = guild_id
        self.voice_client = voice_client
        self._channels = {}

    def add_channel(self, channel):
        self._channels[channel.id] = channel

    def get_channel(self, channel_id):
        return self._channels.get(channel_id)


@pytest.fixture(autouse=True)
def patch_sleep(monkeypatch):
    async def _noop_sleep(_seconds):
        return None

    monkeypatch.setattr(main.asyncio, "sleep", _noop_sleep)


def test_connect_voice_success_first_attempt(monkeypatch):
    guild = FakeGuild(guild_id=10)
    channel = FakeChannel(guild, channel_id=100, outcomes=["success"])

    monkeypatch.setattr(main, "VOICE_CONNECT_RETRIES", 3)
    monkeypatch.setattr(main, "VOICE_RETRY_BACKOFF_SEC", 1)

    vc, error_code = asyncio.run(main.connect_voice_with_retries(guild, channel, reason="test"))

    assert vc is not None
    assert error_code == ""
    assert guild.voice_client is vc
    assert main.last_voice_channel_id[guild.id] == channel.id


def test_connect_voice_timeout_then_success(monkeypatch):
    guild = FakeGuild(guild_id=11)
    channel = FakeChannel(
        guild,
        channel_id=101,
        outcomes=[asyncio.TimeoutError(), "success"],
    )

    monkeypatch.setattr(main, "VOICE_CONNECT_RETRIES", 3)
    monkeypatch.setattr(main, "VOICE_RETRY_BACKOFF_SEC", 1)

    vc, error_code = asyncio.run(main.connect_voice_with_retries(guild, channel, reason="test"))

    assert vc is not None
    assert error_code == ""
    assert main.last_voice_channel_id[guild.id] == channel.id


def test_connect_voice_all_timeouts_returns_timeout_code(monkeypatch):
    guild = FakeGuild(guild_id=12)
    channel = FakeChannel(
        guild,
        channel_id=102,
        outcomes=[asyncio.TimeoutError(), asyncio.TimeoutError(), asyncio.TimeoutError()],
    )

    monkeypatch.setattr(main, "VOICE_CONNECT_RETRIES", 3)
    monkeypatch.setattr(main, "VOICE_RETRY_BACKOFF_SEC", 1)

    vc, error_code = asyncio.run(main.connect_voice_with_retries(guild, channel, reason="test"))

    assert vc is None
    assert error_code == main.VOICE_CONNECT_TIMEOUT_CODE
    assert guild.voice_client is None


def test_connect_voice_handles_stale_voice_client(monkeypatch):
    stale_channel = SimpleNamespace(id=777)
    stale_vc = FakeVoiceClient(channel=stale_channel, connected=False)
    guild = FakeGuild(guild_id=13, voice_client=stale_vc)
    channel = FakeChannel(guild, channel_id=103, outcomes=["success"])

    monkeypatch.setattr(main, "VOICE_CONNECT_RETRIES", 2)
    monkeypatch.setattr(main, "VOICE_RETRY_BACKOFF_SEC", 1)

    vc, error_code = asyncio.run(main.connect_voice_with_retries(guild, channel, reason="test"))

    assert stale_vc.disconnect_called is True
    assert vc is not None
    assert error_code == ""
    assert vc.channel.id == channel.id
