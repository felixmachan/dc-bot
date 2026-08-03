import os
import re
import asyncio
import random
import glob
import shutil
import time
import logging
import unicodedata
import urllib.parse
from dataclasses import dataclass, field
from typing import List, Tuple, Optional, Iterable

import discord
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv

import yt_dlp

try:
    import spotipy
    from spotipy.oauth2 import SpotifyClientCredentials, SpotifyOAuth
except ImportError:
    # spotipy is optional; the bot will still work without Spotify support
    spotipy = None
    SpotifyClientCredentials = None
    SpotifyOAuth = None


load_dotenv()

# --------------------------------------------------------------
# Configuration
# --------------------------------------------------------------
def parse_int_env(name: str, default: int, minimum: int = 1) -> int:
    """Parse positive integer env vars with fallback and warning."""
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
        if value < minimum:
            raise ValueError(f"{name} must be >= {minimum}")
        return value
    except ValueError:
        logging.getLogger("dc_bot").warning(
            "Invalid %s=%r. Using default=%d.", name, raw, default
        )
        return default


def parse_log_level(default: str = "INFO") -> str:
    """Parse BOT_LOG_LEVEL and fallback to INFO on invalid values."""
    raw = (os.getenv("BOT_LOG_LEVEL") or default).upper()
    if raw not in {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}:
        logging.getLogger("dc_bot").warning(
            "Invalid BOT_LOG_LEVEL=%r. Using default=%s.", raw, default
        )
        return default
    return raw
# Bot token and prefix from environment; prefix defaults to '!'
TOKEN = os.getenv("DISCORD_TOKEN")
PREFIX = os.getenv("DISCORD_PREFIX", "!")
GUILD_ID_RAW = os.getenv("DISCORD_GUILD_ID")
GUILD_ID = int(GUILD_ID_RAW) if GUILD_ID_RAW and GUILD_ID_RAW.isdigit() else None
VOICE_CONNECT_TIMEOUT_SEC = parse_int_env("VOICE_CONNECT_TIMEOUT_SEC", 30, minimum=5)
VOICE_CONNECT_RETRIES = parse_int_env("VOICE_CONNECT_RETRIES", 3, minimum=1)
VOICE_RETRY_BACKOFF_SEC = parse_int_env("VOICE_RETRY_BACKOFF_SEC", 2, minimum=1)
SEARCH_TIMEOUT_SEC = parse_int_env("SEARCH_TIMEOUT_SEC", 20, minimum=5)
SEARCH_CANDIDATES = parse_int_env("SEARCH_CANDIDATES", 5, minimum=1)
YOUTUBE_PLAYLIST_LIMIT = parse_int_env("YOUTUBE_PLAYLIST_LIMIT", 50, minimum=1)
# Empty by default: yt-dlp's own client selection is measurably the most reliable
# and the fastest, and it keeps adapting as YouTube changes. Pinning clients here
# (the previous 'android,web') only ever pins us to whatever breaks next, so this
# exists purely as an escape hatch when a specific client must be forced.
YTDLP_PLAYER_CLIENTS = [
    client.strip()
    for client in os.getenv("YTDLP_PLAYER_CLIENTS", "").split(",")
    if client.strip()
]

LOG_LEVEL = parse_log_level()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("dc_bot")

# Spotify credentials (optional)
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")
# Set SPOTIFY_REDIRECT_URI to switch from app-only to user authorisation, which is
# what playlist reads require. Must match a redirect URI registered on the app.
SPOTIFY_REDIRECT_URI = os.getenv("SPOTIFY_REDIRECT_URI")
SPOTIFY_TOKEN_CACHE = os.getenv("SPOTIFY_TOKEN_CACHE", ".spotify-token")
SPOTIFY_SCOPE = "playlist-read-private playlist-read-collaborative"

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix=PREFIX, intents=intents)
last_resync_ts: float = 0.0


@dataclass
class SearchResult:
    """One YouTube search hit, before any media URL has been resolved."""

    url: str
    title: str
    duration: Optional[int] = None
    uploader: Optional[str] = None
    is_live: bool = False
    # Fraction of the query's words found in the title; see match_score.
    score: float = 0.0


@dataclass
class QueuedTrack:
    """One queued song.

    `source` is a YouTube watch URL or a plain search term - never a direct
    media URL. Media URLs are resolved in start_track right before playback,
    because YouTube stream URLs expire (and are IP bound), so anything resolved
    at enqueue time is often dead by the time a long queue reaches it.
    """

    source: str
    title: str
    target: object
    attempts: int = 0
    # Pre-searched fallbacks, tried in order at playback time. Watch URLs do not
    # expire, so caching these is safe; only the media URL is short lived. Empty
    # means "derive from source on first play".
    candidates: List[SearchResult] = field(default_factory=list)


# Per‑guild song queues and currently playing information
song_queue: dict[int, asyncio.Queue] = {}
now_playing: dict[int, str] = {}
current_track: dict[int, QueuedTrack] = {}
playback_locks: dict[int, asyncio.Lock] = {}
voice_reconnect_locks: dict[int, asyncio.Lock] = {}
last_voice_channel_id: dict[int, int] = {}
track_recovery_attempts: dict[int, int] = {}
MAX_TRACK_RECOVERY_ATTEMPTS = 2
# A track that cannot be started after this many queue passes is dropped instead
# of being re-queued forever.
MAX_TRACK_START_ATTEMPTS = 3
# How many search candidates we are willing to fully extract before giving up.
MAX_RESOLVE_CANDIDATES = 3
# Minimum match_score for the top hit to be played without asking. Below this the
# user picks from the results, because something they typed is missing from the
# title and YouTube's ranking has probably drifted onto the wrong song.
SEARCH_CONFIDENT_SCORE = 0.75
# How long the result picker stays clickable.
CHOOSER_TIMEOUT_SEC = 60.0
# Last "now playing" controller message per guild, so the old button set can be
# retired when a new track starts.
player_messages: dict[int, discord.Message] = {}
autocomplete_cache: dict[str, Tuple[float, List[str]]] = {}
AUTOCOMPLETE_CACHE_TTL_SECONDS = 30.0
autocomplete_inflight: dict[str, asyncio.Task] = {}
intentional_voice_disconnect_until: dict[int, float] = {}
VOICE_CONNECT_TIMEOUT_CODE = "VOICE_CONNECT_TIMEOUT"
VOICE_CONNECT_UNSTABLE_CODE = "VOICE_CONNECT_UNSTABLE"
VOICE_INTENTIONAL_DISCONNECT_GRACE_SEC = 15.0


def find_ffmpeg_executable() -> Optional[str]:
    """Locate ffmpeg executable from PATH or common Windows winget locations."""
    ffmpeg_on_path = shutil.which("ffmpeg")
    if ffmpeg_on_path:
        return ffmpeg_on_path

    candidates = [
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Links\ffmpeg.exe"),
        os.path.expandvars(r"%ProgramFiles%\ffmpeg\bin\ffmpeg.exe"),
        r"C:\ffmpeg\bin\ffmpeg.exe",
    ]
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate

    winget_pattern = os.path.expandvars(
        r"%LOCALAPPDATA%\Microsoft\WinGet\Packages\Gyan.FFmpeg_*"
        r"\ffmpeg-*-full_build\bin\ffmpeg.exe"
    )
    matches = glob.glob(winget_pattern)
    if matches:
        matches.sort(reverse=True)
        return matches[0]

    return None


FFMPEG_EXE = find_ffmpeg_executable()


def get_guild_queue(guild_id: int) -> asyncio.Queue:
    """Retrieve or create a queue for a given guild."""
    if guild_id not in song_queue:
        song_queue[guild_id] = asyncio.Queue()
    return song_queue[guild_id]


def get_playback_lock(guild_id: int) -> asyncio.Lock:
    """Retrieve or create a playback lock for a guild."""
    if guild_id not in playback_locks:
        playback_locks[guild_id] = asyncio.Lock()
    return playback_locks[guild_id]


def get_voice_reconnect_lock(guild_id: int) -> asyncio.Lock:
    """Retrieve or create a reconnect lock for a guild."""
    if guild_id not in voice_reconnect_locks:
        voice_reconnect_locks[guild_id] = asyncio.Lock()
    return voice_reconnect_locks[guild_id]


def log_voice_event(
    phase: str,
    guild_id: int,
    channel_id: Optional[int] = None,
    attempt: Optional[int] = None,
    exception: Optional[Exception] = None,
    elapsed_ms: Optional[int] = None,
    level: int = logging.INFO,
):
    """Emit structured logs for voice connect and recovery flow."""
    fields = [
        f"phase={phase}",
        f"guild_id={guild_id}",
        f"channel_id={channel_id if channel_id is not None else 'none'}",
        f"attempt={attempt if attempt is not None else 'none'}",
        f"exception_type={type(exception).__name__ if exception else 'none'}",
        f"elapsed_ms={elapsed_ms if elapsed_ms is not None else 'none'}",
    ]
    logger.log(level, "VOICE_EVENT %s", " ".join(fields))


def mark_intentional_voice_disconnect(guild_id: int, grace_seconds: float = VOICE_INTENTIONAL_DISCONNECT_GRACE_SEC) -> None:
    """Suppress automatic reconnect briefly after manual/expected disconnects."""
    intentional_voice_disconnect_until[guild_id] = time.monotonic() + max(grace_seconds, 1.0)


def is_intentional_voice_disconnect_active(guild_id: int) -> bool:
    """Return True while reconnect suppression window is active."""
    until = intentional_voice_disconnect_until.get(guild_id)
    if not until:
        return False
    if time.monotonic() < until:
        return True
    intentional_voice_disconnect_until.pop(guild_id, None)
    return False


def voice_error_message(error_code: str) -> str:
    """Translate internal voice error codes to user-facing messages."""
    if error_code == VOICE_CONNECT_TIMEOUT_CODE:
        return "Nem sikerült csatlakozni a voice csatornához (hiba: VOICE_CONNECT_TIMEOUT)."
    if error_code == VOICE_CONNECT_UNSTABLE_CODE:
        return "A voice kapcsolat instabil (hiba: VOICE_CONNECT_UNSTABLE)."
    return "Nem sikerült csatlakozni a voice csatornához."


async def connect_voice_with_retries(
    guild: discord.Guild,
    channel: discord.abc.Connectable,
    reason: str,
) -> Tuple[Optional[discord.VoiceClient], str]:
    """Connect or move voice client with deterministic retries and backoff."""
    async def reset_voice_client_state(channel_id: Optional[int], attempt: int) -> None:
        """Force cleanup of partially-initialized voice clients between retries."""
        current_vc = guild.voice_client
        if not current_vc:
            return
        try:
            await current_vc.disconnect(force=True)
        except Exception as cleanup_err:
            log_voice_event(
                "retry_cleanup_disconnect_failed",
                guild.id,
                channel_id=channel_id,
                attempt=attempt,
                exception=cleanup_err,
                level=logging.WARNING,
            )
        try:
            cleanup = getattr(current_vc, "cleanup", None)
            if callable(cleanup):
                cleanup()
        except Exception as cleanup_err:
            log_voice_event(
                "retry_cleanup_finalize_failed",
                guild.id,
                channel_id=channel_id,
                attempt=attempt,
                exception=cleanup_err,
                level=logging.WARNING,
            )

    reconnect_lock = get_voice_reconnect_lock(guild.id)
    last_error_code = VOICE_CONNECT_TIMEOUT_CODE

    async with reconnect_lock:
        for attempt in range(1, VOICE_CONNECT_RETRIES + 1):
            started = time.monotonic()
            vc = guild.voice_client
            channel_id = getattr(channel, "id", None)
            log_voice_event("connect_start", guild.id, channel_id=channel_id, attempt=attempt)
            try:
                if vc and vc.is_connected() and vc.channel and vc.channel.id == channel_id:
                    log_voice_event(
                        "already_connected",
                        guild.id,
                        channel_id=channel_id,
                        attempt=attempt,
                        elapsed_ms=int((time.monotonic() - started) * 1000),
                    )
                    last_voice_channel_id[guild.id] = channel_id
                    return vc, ""

                if vc and vc.is_connected() and vc.channel and vc.channel.id != channel_id:
                    log_voice_event("move_to", guild.id, channel_id=channel_id, attempt=attempt)
                    await vc.move_to(channel)  # type: ignore[arg-type]
                else:
                    if vc and not vc.is_connected():
                        try:
                            await vc.disconnect(force=True)
                        except Exception as stale_err:
                            log_voice_event(
                                "stale_disconnect_failed",
                                guild.id,
                                channel_id=channel_id,
                                attempt=attempt,
                                exception=stale_err,
                                level=logging.WARNING,
                            )
                    await channel.connect(
                        timeout=VOICE_CONNECT_TIMEOUT_SEC,
                        # We handle retries explicitly in this function.
                        reconnect=False,
                        self_deaf=True,
                    )

                await asyncio.sleep(0.6)
                vc = guild.voice_client
                if vc and vc.is_connected() and vc.channel and vc.channel.id == channel_id:
                    elapsed_ms = int((time.monotonic() - started) * 1000)
                    log_voice_event("connect_success", guild.id, channel_id=channel_id, attempt=attempt, elapsed_ms=elapsed_ms)
                    last_voice_channel_id[guild.id] = channel_id
                    return vc, ""

                last_error_code = VOICE_CONNECT_UNSTABLE_CODE
                log_voice_event(
                    "connect_unstable",
                    guild.id,
                    channel_id=channel_id,
                    attempt=attempt,
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                    level=logging.WARNING,
                )
            except asyncio.TimeoutError as timeout_err:
                last_error_code = VOICE_CONNECT_TIMEOUT_CODE
                log_voice_event(
                    "connect_timeout",
                    guild.id,
                    channel_id=channel_id,
                    attempt=attempt,
                    exception=timeout_err,
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                    level=logging.WARNING,
                )
                await reset_voice_client_state(channel_id, attempt)
            except discord.ConnectionClosed as closed_err:
                last_error_code = VOICE_CONNECT_UNSTABLE_CODE
                log_voice_event(
                    "connect_ws_closed",
                    guild.id,
                    channel_id=channel_id,
                    attempt=attempt,
                    exception=closed_err,
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                    level=logging.WARNING,
                )
                await reset_voice_client_state(channel_id, attempt)
            except discord.ClientException as client_err:
                last_error_code = VOICE_CONNECT_UNSTABLE_CODE
                log_voice_event(
                    "connect_client_exception",
                    guild.id,
                    channel_id=channel_id,
                    attempt=attempt,
                    exception=client_err,
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                    level=logging.WARNING,
                )
                await reset_voice_client_state(channel_id, attempt)
            except Exception as conn_err:
                last_error_code = VOICE_CONNECT_UNSTABLE_CODE
                log_voice_event(
                    "connect_exception",
                    guild.id,
                    channel_id=channel_id,
                    attempt=attempt,
                    exception=conn_err,
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                    level=logging.ERROR,
                )
                await reset_voice_client_state(channel_id, attempt)

            if attempt < VOICE_CONNECT_RETRIES:
                await asyncio.sleep(VOICE_RETRY_BACKOFF_SEC * attempt)

    logger.error("Voice connect failed reason=%s guild_id=%s", reason, guild.id)
    return None, last_error_code

def is_url(text: str) -> bool:
    """Check if the provided text looks like a URL."""
    url_pattern = re.compile(r'^(?:http|ftp)s?://|^(?:www\.)', re.IGNORECASE)
    return re.match(url_pattern, text) is not None


def is_youtube_playlist_url(url: str) -> bool:
    """True for a link that means "the whole playlist".

    A watch URL that merely carries a `list=` is the single video the user clicked
    on while a playlist happened to be open, so that stays a single track. Only a
    bare playlist link (no `v=`) queues the lot.
    """
    if not is_url(url):
        return False
    parsed = urllib.parse.urlparse(url)
    if not any(host in parsed.netloc for host in ('youtube.com', 'youtu.be')):
        return False
    params = urllib.parse.parse_qs(parsed.query)
    return bool(params.get('list')) and not params.get('v')


async def get_youtube_playlist(
    url: str, limit: int = YOUTUBE_PLAYLIST_LIMIT
) -> Tuple[List[SearchResult], Optional[str]]:
    """Expand a YouTube playlist link into its entries, newest cap applied."""
    opts = {
        'quiet': True,
        'no_warnings': True,
        # The whole point here, unlike everywhere else in this file.
        'noplaylist': False,
        'extract_flat': True,
        'skip_download': True,
        'playlistend': limit,
    }

    def _extract() -> object:
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)

    try:
        info = await asyncio.wait_for(asyncio.to_thread(_extract), timeout=SEARCH_TIMEOUT_SEC)
    except asyncio.TimeoutError:
        logger.warning("YouTube playlist timeout url=%r", url)
        return [], "timeout"
    except Exception as e:
        logger.warning("YouTube playlist failed url=%r error=%s", url, e)
        return [], "error"

    results: List[SearchResult] = []
    for entry in (info or {}).get('entries') or []:
        if not entry or not is_playable_entry(entry):
            continue
        watch_url = entry.get('url') or entry.get('webpage_url')
        if not watch_url and entry.get('id'):
            watch_url = f"https://www.youtube.com/watch?v={entry['id']}"
        if not watch_url:
            continue
        results.append(SearchResult(
            url=watch_url,
            title=entry.get('title') or 'Ismeretlen',
            duration=entry.get('duration'),
            uploader=entry.get('uploader') or entry.get('channel'),
            is_live=is_live_entry(entry),
            score=1.0,
        ))

    return (results, None) if results else ([], "empty")


YOUTUBE_PLAYLIST_ERRORS = {
    "timeout": "❌ A lejátszási lista beolvasása túl sokáig tartott.",
    "empty": "❌ Ez a lejátszási lista üres, vagy nem érhető el (privát?).",
    "error": "❌ Nem sikerült beolvasni ezt a YouTube lejátszási listát.",
}


def is_spotify_url(url: str) -> bool:
    """Quick check whether a URL points to Spotify content."""
    return 'open.spotify.com' in url


def build_spotify_oauth() -> Optional["SpotifyOAuth"]:
    """Build the user-auth manager, or None when it is not configured."""
    if not SpotifyOAuth or not SPOTIFY_REDIRECT_URI:
        return None
    if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
        return None
    return SpotifyOAuth(
        client_id=SPOTIFY_CLIENT_ID,
        client_secret=SPOTIFY_CLIENT_SECRET,
        redirect_uri=SPOTIFY_REDIRECT_URI,
        scope=SPOTIFY_SCOPE,
        cache_path=SPOTIFY_TOKEN_CACHE,
        # The bot is headless; authorisation is a separate, deliberate step.
        open_browser=False,
    )


def create_spotify_client() -> Optional[spotipy.Spotify]:
    """Create a Spotify client, preferring user auth when it has been set up.

    App-only credentials cannot read playlist contents - Spotify answers 403 for
    a development-mode app - so user auth is the only route to playlists. It is
    optional: without a cached token the bot falls back to app-only auth, which
    still covers track and album links, rather than refusing to start.
    """
    if not spotipy or not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
        return None

    oauth = build_spotify_oauth()
    if oauth:
        try:
            cached = oauth.cache_handler.get_cached_token()
        except Exception as cache_err:
            cached = None
            logger.warning("Spotify token cache unreadable error=%s", cache_err)
        if cached:
            logger.info("Spotify: felhasznaloi hitelesites aktiv (playlistek olvashatok)")
            return spotipy.Spotify(auth_manager=oauth)
        logger.warning(
            "Spotify: SPOTIFY_REDIRECT_URI be van allitva, de nincs token a %r fajlban. "
            "Futtasd: python tools/spotify_authorize.py",
            SPOTIFY_TOKEN_CACHE,
        )

    logger.info("Spotify: app-only hitelesites (szam es album megy, playlist nem)")
    return spotipy.Spotify(
        auth_manager=SpotifyClientCredentials(
            client_id=SPOTIFY_CLIENT_ID, client_secret=SPOTIFY_CLIENT_SECRET
        )
    )


SPOTIFY_CLIENT = create_spotify_client()

# --------------------------------------------------------------
# Slash command helpers and autocomplete
# --------------------------------------------------------------
async def yt_autocomplete(
    interaction: discord.Interaction,
    current: str
) -> List[app_commands.Choice[str]]:
    """
    Provide autocomplete suggestions for the play slash command.

    This function uses yt_dlp to search for the current user input and returns
    up to five song titles as choices. If yt_dlp is unavailable or an error
    occurs, an empty list is returned.

    Parameters
    ----------
    interaction: discord.Interaction
        The interaction that triggered the autocomplete. Unused here but
        required by the signature.
    current: str
        The text the user has typed so far.

    Returns
    -------
    List[app_commands.Choice[str]]
        A list of up to five choices containing song titles.
    """
    # Keep autocomplete fast: short inputs are often noisy and increase timeout risk.
    query = current.strip()
    if len(query) < 2:
        return []
    cache_key = query.lower()
    now = time.monotonic()
    cached = autocomplete_cache.get(cache_key)
    if cached and now - cached[0] < AUTOCOMPLETE_CACHE_TTL_SECONDS:
        return [app_commands.Choice(name=title, value=title) for title in cached[1]]

    # Prepare search options: flat extraction to avoid deep info and limit results
    search_opts = {
        'format': 'bestaudio/best',
        'quiet': True,
        'noplaylist': True,
        'extract_flat': True,
        'skip_download': True,
    }

    def _fetch_titles() -> List[str]:
        with yt_dlp.YoutubeDL(search_opts) as ydl:
            info = ydl.extract_info(f"ytsearch5:{query}", download=False)
        titles: List[str] = []
        for entry in info.get('entries', []):
            title = entry.get('title')
            if title:
                # Discord choice name/value length safety.
                titles.append(title[:100])
            if len(titles) >= 5:
                break
        return titles

    async def _populate_cache() -> None:
        try:
            # Populate cache in the background so autocomplete response stays immediate.
            titles = await asyncio.wait_for(asyncio.to_thread(_fetch_titles), timeout=4.0)
            autocomplete_cache[cache_key] = (time.monotonic(), titles)
        except Exception:
            pass
        finally:
            autocomplete_inflight.pop(cache_key, None)

    # Never block autocomplete on network I/O; return fast to avoid 10062 Unknown interaction.
    if cache_key not in autocomplete_inflight:
        autocomplete_inflight[cache_key] = asyncio.create_task(_populate_cache())
    return []


# Premieres that have not started and streams still being processed have no media
# to read. Actual live streams are playable, so they are only deprioritised.
UNPLAYABLE_LIVE_STATUS = {'is_upcoming', 'post_live'}


def is_playable_entry(entry: dict) -> bool:
    """Reject only entries with no readable media, e.g. an unstarted premiere."""
    return entry.get('live_status') not in UNPLAYABLE_LIVE_STATUS


def is_live_entry(entry: dict) -> bool:
    """True for an in-progress live stream (playable, but a poor match for a song search)."""
    return bool(entry.get('is_live')) or entry.get('live_status') == 'is_live'


def fold_text(text: str) -> str:
    """Lowercase, strip accents and punctuation, so 'SZÍVTIPRÓ' matches 'szivtipro'."""
    decomposed = unicodedata.normalize('NFKD', text or '')
    stripped = ''.join(ch for ch in decomposed if not unicodedata.combining(ch))
    return re.sub(r'[^0-9a-z]+', ' ', stripped.lower()).strip()


def match_score(query: str, title: str) -> float:
    """Fraction of the query's words that appear in the title, 0.0 to 1.0.

    Spaces are ignored on the title side, so a run-together query like 'tdanny'
    still matches 'T. Danny'. This is what rescues searches where YouTube's own
    ranking puts the wanted song below unrelated tracks by the same artist.
    """
    tokens = fold_text(query).split()
    if not tokens:
        return 0.0
    haystack = fold_text(title).replace(' ', '')
    matched = sum(1 for token in tokens if token in haystack)
    return matched / len(tokens)


async def search_youtube(term: str, limit: int = SEARCH_CANDIDATES) -> List["SearchResult"]:
    """Return up to `limit` candidates for a search term, best match first.

    This deliberately uses a flat search: it is one cheap request and it never
    fails just because the top hit happens to be region blocked, age gated or a
    live stream. Picking a usable candidate is left to resolve_stream_url.
    """
    if is_url(term):
        return [SearchResult(url=term, title=term, score=1.0)]

    search_opts = {
        'quiet': True,
        'no_warnings': True,
        'noplaylist': True,
        'extract_flat': True,
        'skip_download': True,
    }

    def _search() -> object:
        with yt_dlp.YoutubeDL(search_opts) as ydl:
            return ydl.extract_info(f"ytsearch{limit}:{term}", download=False)

    try:
        info = await asyncio.wait_for(asyncio.to_thread(_search), timeout=SEARCH_TIMEOUT_SEC)
    except asyncio.TimeoutError:
        logger.warning("yt-dlp search timeout term=%r timeout=%ds", term, SEARCH_TIMEOUT_SEC)
        return []
    except Exception as e:
        logger.error("yt-dlp search error term=%r error=%s", term, e)
        return []

    candidates: List[SearchResult] = []
    for entry in (info or {}).get('entries', []) or []:
        if not entry or not is_playable_entry(entry):
            continue
        watch_url = entry.get('url') or entry.get('webpage_url')
        video_id = entry.get('id')
        if not watch_url and video_id:
            watch_url = f"https://www.youtube.com/watch?v={video_id}"
        if not watch_url:
            continue
        title = entry.get('title') or 'Ismeretlen'
        candidates.append(SearchResult(
            url=watch_url,
            title=title,
            duration=entry.get('duration'),
            uploader=entry.get('uploader') or entry.get('channel'),
            is_live=is_live_entry(entry),
            score=match_score(term, title),
        ))

    # Stable sort keeps YouTube's own relevance order among equally good matches,
    # while pushing 24/7 streams behind real uploads and promoting titles that
    # actually contain what was typed. A search for a song should not land on a
    # radio stream, but "lofi hip hop radio" must still work when a stream is all
    # there is.
    candidates.sort(key=lambda c: (c.is_live, -c.score))

    if not candidates:
        logger.warning("yt-dlp search returned no usable candidate term=%r", term)
    else:
        logger.debug("yt-dlp search term=%r candidates=%d", term, len(candidates))
    return candidates


async def resolve_stream_url(candidates: List[SearchResult]) -> Optional[Tuple[str, str]]:
    """Resolve the first playable candidate to a fresh (stream_url, title).

    Called right before playback so the media URL is as young as possible, and it
    walks the candidate list, so one broken video does not sink the whole request.
    """
    if not candidates:
        return None

    ydl_opts = {
        'format': 'bestaudio[ext=m4a]/bestaudio[acodec!=none]/best',
        'quiet': True,
        'no_warnings': True,
        'noplaylist': True,
        'extract_flat': False,
        'skip_download': True,
    }
    if YTDLP_PLAYER_CLIENTS:
        ydl_opts['extractor_args'] = {'youtube': {'player_client': YTDLP_PLAYER_CLIENTS}}

    def _extract(url: str) -> object:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(url, download=False)

    last_error: Optional[str] = None
    # Cap the retries: every failed candidate costs a full extraction round trip,
    # and dead air while we grind through five of them is worse than failing fast.
    for candidate in candidates[:MAX_RESOLVE_CANDIDATES]:
        watch_url = candidate.url
        try:
            info = await asyncio.wait_for(
                asyncio.to_thread(_extract, watch_url), timeout=SEARCH_TIMEOUT_SEC
            )
        except asyncio.TimeoutError:
            last_error = "timeout"
            logger.warning("yt-dlp resolve timeout url=%r", watch_url)
            continue
        except Exception as e:
            last_error = str(e)
            logger.warning("yt-dlp resolve failed url=%r error=%s", watch_url, e)
            continue

        entries = (info or {}).get('entries') or [info]
        entry = entries[0] if entries else None
        if not entry or not is_playable_entry(entry):
            last_error = "not playable (unstarted premiere / still processing)"
            continue

        audio_url = entry.get('url')
        if not audio_url:
            audio_formats = [
                f for f in entry.get('formats', [])
                if f.get('acodec') != 'none'
                and f.get('vcodec') == 'none'
                and f.get('url')
                and f.get('protocol') not in {'m3u8', 'http_dash_segments'}
            ]
            if not audio_formats:
                last_error = "no audio-only format"
                logger.warning("no usable audio format url=%r", watch_url)
                continue
            audio_formats.sort(key=lambda f: f.get('abr') or f.get('asr') or 0, reverse=True)
            audio_url = audio_formats[0]['url']

        return audio_url, entry.get('title') or candidate.title

    logger.error(
        "resolve failed for all %d candidate(s) first=%r last_error=%s",
        len(candidates), candidates[0].url, last_error,
    )
    return None


async def resolve_track(track: QueuedTrack) -> Optional[Tuple[str, str]]:
    """Resolve a queued track to a playable (stream_url, title)."""
    if not track.candidates:
        # Spotify entries and prefix-command URLs arrive without a candidate list.
        track.candidates = (
            [SearchResult(url=track.source, title=track.title, score=1.0)]
            if is_url(track.source)
            else await search_youtube(track.source)
        )
    return await resolve_stream_url(track.candidates)


def parse_spotify_id(url: str) -> Optional[Tuple[str, str]]:
    """Extract Spotify content type and ID from a URL."""
    # Match patterns like /track/{id}, /playlist/{id}, /album/{id}
    match = re.search(r'open\.spotify\.com/(track|playlist|album)/([A-Za-z0-9]+)', url)
    if not match:
        return None
    return match.group(1), match.group(2)


MAX_SPOTIFY_TRACKS = 50

SPOTIFY_ERROR_MESSAGES = {
    "no_client": "❌ A Spotify támogatás nincs beállítva (hiányzó SPOTIFY_CLIENT_ID/SECRET).",
    "bad_url": "❌ Ezt a Spotify linket nem ismerem fel. Szám, album vagy lejátszási lista linkje kell.",
    "forbidden": (
        "❌ A Spotify egyetlen lejátszási lista tartalmát sem adja ki ennek a botnak — "
        "publikusét sem.\n"
        "➡️ Ami megy: Spotify **album**- és **szám**-link, vagy egy **YouTube lejátszási lista** linkje."
    ),
    "not_found": (
        "❌ Ezt a listát a Spotify egyáltalán nem mutatja a botoknak: az algoritmikus és a "
        "Spotify által készített listák (Daily Mix, rádió, Neked készült) le vannak zárva.\n"
        "➡️ Ami megy: Spotify **album**- és **szám**-link, vagy egy **YouTube lejátszási lista** linkje."
    ),
    "empty": "❌ Ez a Spotify tartalom üres.",
    "error": "❌ Nem sikerült beolvasni a Spotify tartalmat.",
}


def spotify_error_message(code: str) -> str:
    return SPOTIFY_ERROR_MESSAGES.get(code, SPOTIFY_ERROR_MESSAGES["error"])


async def get_spotify_tracks(url: str) -> Tuple[List[str], Optional[str]]:
    """Resolve a Spotify URL to search terms, plus an error code when it fails.

    The error code matters: Spotify answers 404 for its own algorithmic playlists
    and 403 for everyone else's playlist contents, and the two need different
    advice. Collapsing them into "empty or unreadable" sent people chasing the
    wrong problem.
    """
    if not SPOTIFY_CLIENT:
        return [], "no_client"
    parsed = parse_spotify_id(url)
    if not parsed:
        return [], "bad_url"
    content_type, spotify_id = parsed

    def _describe(name: str, artists: Iterable[dict]) -> str:
        return f"{name} {', '.join(a['name'] for a in artists)}"

    def _fetch_tracks() -> List[str]:
        local_result: List[str] = []
        if content_type == 'track':
            track = SPOTIFY_CLIENT.track(spotify_id)
            local_result.append(_describe(track['name'], track['artists']))
        elif content_type == 'album':
            page = SPOTIFY_CLIENT.album_tracks(spotify_id, limit=50)
            while page:
                for item in page['items']:
                    local_result.append(_describe(item['name'], item['artists']))
                    if len(local_result) >= MAX_SPOTIFY_TRACKS:
                        return local_result
                # Albums longer than one page were silently truncated before.
                page = SPOTIFY_CLIENT.next(page) if page.get('next') else None
        elif content_type == 'playlist':
            page = SPOTIFY_CLIENT.playlist_items(
                spotify_id,
                fields='next,items(track(name,artists(name)))',
                additional_types=['track'],
                limit=50,
            )
            while page:
                for item in page['items']:
                    track = item.get('track')
                    if not track:
                        continue
                    local_result.append(_describe(track['name'], track['artists']))
                    if len(local_result) >= MAX_SPOTIFY_TRACKS:
                        return local_result
                page = SPOTIFY_CLIENT.next(page) if page.get('next') else None
        return local_result

    try:
        result = await asyncio.wait_for(asyncio.to_thread(_fetch_tracks), timeout=SEARCH_TIMEOUT_SEC)
    except asyncio.TimeoutError:
        logger.warning("Spotify fetch timeout url=%r", url)
        return [], "error"
    except Exception as e:
        status = getattr(e, "http_status", None)
        logger.warning("Spotify fetch failed url=%r status=%s error=%s", url, status, e)
        if status == 403:
            return [], "forbidden"
        if status == 404:
            return [], "not_found"
        return [], "error"

    return result, None if result else "empty"


@bot.event
async def on_ready():
    logger.info("Bot elindult user=%s", bot.user)
    # Register the control panel once so buttons on messages from a previous
    # process keep working after a restart.
    bot.add_view(PlayerView())
    try:
        # Keep a global registration for portability across guilds.
        # If a guild ID is configured, sync that too for faster propagation there.
        global_synced = await bot.tree.sync()
        logger.info("Global slash sync kesz count=%d", len(global_synced))
        if GUILD_ID:
            guild_obj = discord.Object(id=GUILD_ID)
            guild_synced = await bot.tree.sync(guild=guild_obj)
            logger.info("Guild slash sync kesz count=%d guild_id=%s", len(guild_synced), GUILD_ID)
    except Exception as e:
        logger.error("Slash parancs sync sikertelen error=%s", e)


async def safe_send(
    target: object,
    message: Optional[str] = None,
    *,
    embed: Optional[discord.Embed] = None,
    view: Optional[discord.ui.View] = None,
) -> Optional[discord.Message]:
    """Send to either a Context or an Interaction target, returning the sent message."""
    kwargs: dict = {}
    if message is not None:
        kwargs["content"] = message
    if embed is not None:
        kwargs["embed"] = embed
    if view is not None:
        kwargs["view"] = view

    # Context has .send; Interaction does not. Resolve the attribute separately so a
    # genuine AttributeError raised inside the send call is not mistaken for the
    # wrong target type.
    sender = getattr(target, "send", None)
    if sender is not None:
        try:
            return await sender(**kwargs)
        except Exception as send_err:
            logger.warning("safe_send failed error=%s", send_err)
            return None

    # For interactions, send to the channel rather than the followup webhook: these
    # announcements fire while a long queue plays, and an interaction token dies
    # after 15 minutes.
    channel = getattr(target, "channel", None)
    if channel is not None:
        try:
            return await channel.send(**kwargs)
        except Exception as send_err:
            logger.warning("safe_send channel send failed error=%s", send_err)

    try:
        return await target.followup.send(wait=True, **kwargs)  # type: ignore[attr-defined]
    except Exception as send_err:
        logger.warning("safe_send failed error=%s", send_err)
        return None


async def ensure_voice_connection(guild: discord.Guild, target: Optional[object] = None) -> Optional[discord.VoiceClient]:
    """Ensure active voice connection, attempting reconnect to last known channel."""
    if is_intentional_voice_disconnect_active(guild.id):
        return None
    vc = guild.voice_client
    if vc and vc.is_connected():
        if vc.channel:
            last_voice_channel_id[guild.id] = vc.channel.id
        return vc

    channel_id = last_voice_channel_id.get(guild.id)
    channel = guild.get_channel(channel_id) if channel_id else None
    if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
        return None

    vc, error_code = await connect_voice_with_retries(guild, channel, reason="recovery")
    if vc and target:
        await safe_send(target, "🔁 Kapcsolat megszakadt, újracsatlakoztam a voice csatornához.")
    if not vc:
        log_voice_event("recovery_failed", guild.id, channel_id=channel_id, level=logging.WARNING)
    return vc



def queued_titles(guild_id: int) -> List[str]:
    """Snapshot upcoming titles without consuming the queue."""
    queue = get_guild_queue(guild_id)
    return [track.title for track in list(queue._queue)]  # type: ignore[attr-defined]


def build_player_embed(guild_id: int, track: QueuedTrack) -> discord.Embed:
    """Build the now-playing embed that carries the control buttons."""
    embed = discord.Embed(
        title="🎶 Most játszom",
        description=f"**{track.title}**",
        color=discord.Color.blurple(),
    )
    if is_url(track.source):
        embed.url = track.source

    upcoming = queued_titles(guild_id)
    if upcoming:
        preview = "\n".join(f"{idx}. {title}" for idx, title in enumerate(upcoming[:3], start=1))
        if len(upcoming) > 3:
            preview += f"\n…és még {len(upcoming) - 3} további."
        embed.add_field(name=f"Következik ({len(upcoming)})", value=preview, inline=False)
    else:
        embed.add_field(name="Következik", value="A várólista üres.", inline=False)
    return embed


class PlayerView(discord.ui.View):
    """Persistent control panel attached to the now-playing embed.

    Stateless on purpose: every handler reads the guild off the interaction, so a
    single instance registered in on_ready keeps working after a bot restart.
    """

    def __init__(self):
        super().__init__(timeout=None)

    async def _voice_client(self, interaction: discord.Interaction) -> Optional[discord.VoiceClient]:
        """Return the voice client if the clicking user is allowed to control it."""
        vc = interaction.guild.voice_client if interaction.guild else None
        if not vc or not vc.is_connected():
            await interaction.response.send_message("ℹ️ Nem vagyok voice csatornában.", ephemeral=True)
            return None
        user_voice = getattr(interaction.user, "voice", None)
        if not user_voice or user_voice.channel != vc.channel:
            await interaction.response.send_message(
                "❗ Ehhez ugyanabban a hangcsatornában kell lenned, mint a bot.", ephemeral=True
            )
            return None
        return vc

    @discord.ui.button(emoji="⏯️", label="Pause/Resume", style=discord.ButtonStyle.secondary, custom_id="player:playpause")
    async def playpause(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = await self._voice_client(interaction)
        if not vc:
            return
        if vc.is_playing():
            vc.pause()
            await interaction.response.send_message("⏸️ Szüneteltetve.", ephemeral=True)
        elif vc.is_paused():
            vc.resume()
            await interaction.response.send_message("▶️ Folytatva.", ephemeral=True)
        else:
            await interaction.response.send_message("ℹ️ Nem játszik semmi.", ephemeral=True)

    @discord.ui.button(emoji="⏭️", label="Skip", style=discord.ButtonStyle.primary, custom_id="player:skip")
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = await self._voice_client(interaction)
        if not vc:
            return
        if vc.is_playing() or vc.is_paused():
            # stop() fires the after callback, which advances the queue.
            vc.stop()
            await interaction.response.send_message(
                f"⏭️ {interaction.user.display_name} kihagyta az aktuális számot."
            )
        else:
            await interaction.response.send_message("ℹ️ Nem játszik semmi.", ephemeral=True)

    @discord.ui.button(emoji="⏹️", label="Stop", style=discord.ButtonStyle.danger, custom_id="player:stop")
    async def stop_playback(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = await self._voice_client(interaction)
        if not vc:
            return
        clear_queue(interaction.guild.id)
        if vc.is_playing() or vc.is_paused():
            vc.stop()
        now_playing.pop(interaction.guild.id, None)
        current_track.pop(interaction.guild.id, None)
        track_recovery_attempts.pop(interaction.guild.id, None)
        await interaction.response.send_message(
            f"⏹️ {interaction.user.display_name} leállította a lejátszást, a várólista törölve."
        )

    @discord.ui.button(emoji="🔀", label="Shuffle", style=discord.ButtonStyle.secondary, custom_id="player:shuffle")
    async def shuffle(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = await self._voice_client(interaction)
        if not vc:
            return
        queue = get_guild_queue(interaction.guild.id)
        items = list(queue._queue)  # type: ignore[attr-defined]
        if len(items) < 2:
            await interaction.response.send_message("ℹ️ Nincs elég szám a keveréshez.", ephemeral=True)
            return
        random.shuffle(items)
        queue._queue.clear()  # type: ignore[attr-defined]
        for item in items:
            queue.put_nowait(item)
        await interaction.response.send_message("🔀 A várólista megkeverve.", ephemeral=True)

    @discord.ui.button(emoji="📜", label="Queue", style=discord.ButtonStyle.secondary, custom_id="player:queue")
    async def show_queue(self, interaction: discord.Interaction, button: discord.ui.Button):
        titles = queued_titles(interaction.guild.id)
        if not titles:
            await interaction.response.send_message("ℹ️ A várólista üres.", ephemeral=True)
            return
        lines = [f"{idx}. {title}" for idx, title in enumerate(titles[:10], start=1)]
        if len(titles) > 10:
            lines.append(f"…és még {len(titles) - 10} további.")
        await interaction.response.send_message(
            f"**Várólista ({len(titles)} szám):**\n" + "\n".join(lines), ephemeral=True
        )


def build_info_embed() -> discord.Embed:
    """Full command and button reference, with the real prefix and group name filled in."""
    # Read the group name off the command itself so the help can never drift from
    # what is actually registered.
    group = music_group.name
    embed = discord.Embed(
        title="🎧 musicBOT — súgó",
        description=(
            f"Minden parancs kétféleképp is megy: **`/{group} <parancs>`** vagy "
            f"prefixszel **`{PREFIX}<parancs>`**.\n"
            "Lejátszáshoz előbb lépj be egy hangcsatornába."
        ),
        color=discord.Color.blurple(),
    )

    embed.add_field(
        name="▶️ Lejátszás",
        value=(
            f"**`/{group} play <szám>`** • `{PREFIX}play <szám>`\n"
            "Keres és lejátszik. Ha már szól valami, a várólista végére kerül.\n"
            "Elfogad: **szám címét**, **YouTube linket**, "
            f"**YouTube lejátszási listát** (max. {YOUTUBE_PLAYLIST_LIMIT} szám), "
            "valamint **Spotify szám- és album-linket**.\n"
            "⚠️ Spotify **lejátszási lista** nem megy — azt a Spotify nem adja ki a botoknak. "
            "Használj helyette YouTube listát."
        ),
        inline=False,
    )
    embed.add_field(
        name="⏯️ Vezérlés",
        value=(
            f"**`/{group} pause`** • `{PREFIX}pause` — szünet\n"
            f"**`/{group} resume`** • `{PREFIX}resume` — folytatás\n"
            f"**`/{group} skip`** • `{PREFIX}skip` — következő szám\n"
            f"**`/{group} stop`** • `{PREFIX}stop` — leállítás és a várólista törlése"
        ),
        inline=False,
    )
    embed.add_field(
        name="📜 Várólista",
        value=(
            f"**`/{group} queue`** • `{PREFIX}queue` — mi jön még\n"
            f"**`/{group} shuffle`** • `{PREFIX}shuffle` — a várólista megkeverése\n"
            f"**`/{group} np`** • `{PREFIX}np` — mi szól most"
        ),
        inline=False,
    )
    embed.add_field(
        name="🔌 Csatlakozás",
        value=(
            f"**`/{group} join`** • `{PREFIX}join` — belép a csatornádba\n"
            f"**`/{group} leave`** • `{PREFIX}leave` — kilép és törli a várólistát\n"
            "A bot magától is belép, ha a `play`-t használod, és kilép, ha elfogy a lista."
        ),
        inline=False,
    )
    embed.add_field(
        name="🎛️ A lejátszó gombjai",
        value=(
            "Minden elinduló szám alatt megjelenik a vezérlőpult:\n"
            "⏯️ **Pause/Resume** — szünet vagy folytatás\n"
            "⏭️ **Skip** — ugrás a következő számra\n"
            "⏹️ **Stop** — leállítás és a várólista törlése\n"
            "🔀 **Shuffle** — a várólista megkeverése\n"
            "📜 **Queue** — a várólista (csak neked látszik)\n\n"
            "A gombok csak akkor működnek, ha **ugyanabban a hangcsatornában vagy, "
            "mint a bot**. Újraindítás után is élnek."
        ),
        inline=False,
    )
    embed.add_field(
        name="🤔 Ha rákérdez, melyik szám kell",
        value=(
            "Ha a találat nem egyértelmű — például elgépelted, vagy a keresett szó "
            "egyik cím sem tartalmazza —, a bot feldobja az 5 legjobb találatot "
            "hosszal és csatornával.\n"
            "Válassz az **1️⃣–5️⃣** gombokkal, vagy **✖️ Mégse**. "
            f"Csak az választhat, aki a keresést indította, és {int(CHOOSER_TIMEOUT_SEC)} "
            "másodpercig él a kérdés."
        ),
        inline=False,
    )
    embed.set_footer(
        text=f"Tipp: a /{group} play mezőben gépelés közben javaslatokat is kapsz."
    )
    return embed


def format_duration(seconds: Optional[int]) -> str:
    """Render a track length as m:ss, or h:mm:ss past an hour."""
    if not seconds:
        return "?"
    seconds = int(seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


NUMBER_EMOJI = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣"]


def build_chooser_embed(query: str, results: List[SearchResult]) -> discord.Embed:
    """List the search hits so the user can pick the right one."""
    embed = discord.Embed(
        title="🤔 Melyik legyen?",
        description=f"Nem egyértelmű, mit keresel: **{query}**",
        color=discord.Color.orange(),
    )
    for idx, result in enumerate(results):
        who = result.uploader or "ismeretlen csatorna"
        length = "🔴 élő" if result.is_live else format_duration(result.duration)
        embed.add_field(
            name=f"{NUMBER_EMOJI[idx]} {result.title[:100]}",
            value=f"{who} • {length}",
            inline=False,
        )
    embed.set_footer(text=f"Válassz {int(CHOOSER_TIMEOUT_SEC)} másodpercen belül.")
    return embed


class TrackChooserView(discord.ui.View):
    """Numbered picker shown when the best search hit is not convincing.

    Short lived and tied to one requester, so unlike PlayerView it carries state
    and is not registered for persistence: after a restart the buttons are dead,
    which is fine for a 60 second prompt.
    """

    def __init__(self, query: str, results: List[SearchResult], requester_id: int, target: object):
        super().__init__(timeout=CHOOSER_TIMEOUT_SEC)
        self.query = query
        self.results = results
        self.requester_id = requester_id
        self.target = target
        self.message: Optional[discord.Message] = None

        for idx in range(len(results)):
            button = discord.ui.Button(
                emoji=NUMBER_EMOJI[idx],
                style=discord.ButtonStyle.primary,
                custom_id=f"chooser:pick:{idx}",
            )
            button.callback = self._make_pick_callback(idx)
            self.add_item(button)

        cancel = discord.ui.Button(
            emoji="✖️", label="Mégse", style=discord.ButtonStyle.secondary, custom_id="chooser:cancel"
        )
        cancel.callback = self._cancel
        self.add_item(cancel)

    async def _check_requester(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "❗ Ezt a keresést nem te indítottad.", ephemeral=True
            )
            return False
        return True

    def _make_pick_callback(self, index: int):
        async def callback(interaction: discord.Interaction):
            if not await self._check_requester(interaction):
                return
            chosen = self.results[index]
            self.stop()
            await interaction.response.edit_message(
                content=f"✅ Választva: **{chosen.title}**", embed=None, view=None
            )
            # Keep the remaining hits as fallbacks, with the chosen one first, so a
            # dead video still recovers without asking again.
            ordered = [chosen] + [r for i, r in enumerate(self.results) if i != index]
            track = QueuedTrack(
                source=chosen.url,
                title=chosen.title,
                target=self.target,
                candidates=ordered,
            )
            await enqueue_track(interaction.guild, track)

        return callback

    async def _cancel(self, interaction: discord.Interaction):
        if not await self._check_requester(interaction):
            return
        self.stop()
        await interaction.response.edit_message(content="❌ Megszakítva.", embed=None, view=None)

    async def on_timeout(self) -> None:
        if not self.message:
            return
        try:
            await self.message.edit(
                content="⌛ Lejárt a választás ideje.", embed=None, view=None
            )
        except Exception as edit_err:
            logger.debug("Chooser timeout edit failed error=%s", edit_err)


async def enqueue_track(guild: discord.Guild, track: QueuedTrack) -> None:
    """Add a track to the guild queue and start playback if nothing is running."""
    queue = get_guild_queue(guild.id)
    await queue.put(track)
    vc = guild.voice_client
    if vc and not vc.is_playing() and not vc.is_paused():
        await play_next(guild)


async def send_track_chooser(
    guild: discord.Guild,
    query: str,
    results: List[SearchResult],
    requester_id: int,
    target: object,
) -> None:
    """Ask the user which hit they meant."""
    view = TrackChooserView(query, results, requester_id, target)
    embed = build_chooser_embed(query, results)
    message = await safe_send(target, embed=embed, view=view)
    if message:
        view.message = message
        return

    # No Embed Links permission: fall back to playing the best guess rather than
    # leaving the request silently unanswered.
    view.stop()
    logger.warning("Chooser could not be sent; playing top hit for %r", query)
    top = results[0]
    await safe_send(target, f"🎶 Nem tudtam listát küldeni, ezt indítom: **{top.title}**")
    await enqueue_track(
        guild,
        QueuedTrack(source=top.url, title=top.title, target=target, candidates=results),
    )


async def send_player_message(guild: discord.Guild, track: QueuedTrack) -> None:
    """Replace the previous control panel with a fresh one for the new track."""
    await retire_player_message(guild.id)
    embed = build_player_embed(guild.id, track)
    message = await safe_send(track.target, embed=embed, view=PlayerView())
    if message:
        player_messages[guild.id] = message
        return

    # Sending the panel needs the Embed Links permission; without it the announcement
    # would vanish entirely, which is worse than the plain line it replaced.
    logger.warning(
        "Player panel could not be sent guild_id=%s; falling back to plain text", guild.id
    )
    await safe_send(track.target, f"🎶 Most játszom: **{track.title}**")


async def retire_player_message(guild_id: int) -> None:
    """Strip the buttons off the previous now-playing message so only one panel is live."""
    message = player_messages.pop(guild_id, None)
    if not message:
        return
    try:
        await message.edit(view=None)
    except Exception as edit_err:
        logger.debug("Player message retire failed error=%s", edit_err)


def clear_queue(guild_id: int) -> None:
    """Drop every queued track for a guild."""
    queue = get_guild_queue(guild_id)
    while not queue.empty():
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            break


async def start_track(guild: discord.Guild, track: QueuedTrack, announce: bool = True) -> bool:
    """Resolve a fresh media URL and start playing one track."""
    vc = await ensure_voice_connection(guild, track.target)
    if not vc:
        return False
    if not FFMPEG_EXE:
        logger.error("FFmpeg nincs telepitve vagy nem talalhato.")
        return False

    # Resolved here rather than at enqueue time: YouTube media URLs expire.
    resolved = await resolve_track(track)
    if not resolved:
        logger.warning("Nem sikerult feloldani a szamot source=%r", track.source)
        return False
    stream_url, resolved_title = resolved
    track.title = resolved_title

    source = discord.FFmpegPCMAudio(
        stream_url,
        executable=FFMPEG_EXE,
        before_options=(
            "-reconnect 1 -reconnect_streamed 1 -reconnect_at_eof 1 "
            "-reconnect_on_network_error 1 -reconnect_delay_max 5 -nostdin"
        ),
        options="-vn -loglevel panic"
    )

    def after(error):
        fut = asyncio.run_coroutine_threadsafe(handle_track_end(guild, error), bot.loop)
        try:
            fut.result()
        except Exception as exc:
            logger.error("Track end callback hiba error=%s", exc)

    try:
        vc.play(source, after=after)
    except Exception as play_err:
        logger.error("Lejatszas inditasi hiba error=%s", play_err)
        source.cleanup()
        return False

    now_playing[guild.id] = track.title
    current_track[guild.id] = track
    if announce:
        await send_player_message(guild, track)
    return True


async def handle_track_end(guild: discord.Guild, error: Optional[Exception]):
    """Handle playback completion and retry current track on transient errors."""
    if error:
        logger.warning("Lejatszasi hiba error=%s", error)
        track = current_track.get(guild.id)
        if track:
            attempts = track_recovery_attempts.get(guild.id, 0)
            if attempts < MAX_TRACK_RECOVERY_ATTEMPTS:
                track_recovery_attempts[guild.id] = attempts + 1
                await asyncio.sleep(1.5)
                # start_track re-resolves the media URL, which also recovers from
                # an expired stream URL rather than just retrying the dead one.
                ok = await start_track(guild, track, announce=False)
                if ok:
                    return

    current_track.pop(guild.id, None)
    track_recovery_attempts.pop(guild.id, None)
    await play_next(guild)


async def retry_play_next_later(guild: discord.Guild, delay_seconds: float = 2.0):
    """Retry queue playback later to avoid stalling on transient start failures."""
    await asyncio.sleep(delay_seconds)
    if is_intentional_voice_disconnect_active(guild.id):
        return
    await play_next(guild)


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    global last_resync_ts
    if isinstance(error, app_commands.CommandInvokeError) and isinstance(error.original, discord.NotFound):
        # Interaction token likely expired; avoid noisy logs.
        return
    if isinstance(error, app_commands.CommandNotFound):
        now = asyncio.get_running_loop().time()
        # Prevent sync storms if many users invoke stale slash commands.
        if now - last_resync_ts > 60:
            last_resync_ts = now
            try:
                if interaction.guild:
                    await bot.tree.sync(guild=interaction.guild)
                await bot.tree.sync()
            except Exception as sync_err:
                logger.warning("Automatikus slash re-sync sikertelen error=%s", sync_err)
        msg = "A slash parancsok frissülnek. Próbáld újra pár másodperc múlva."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
        return
    logger.error("App command hiba error=%s", error)


@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    # Keep last known channel for bot reconnect attempts.
    if not bot.user or member.id != bot.user.id:
        return
    guild = member.guild
    if after.channel:
        last_voice_channel_id[guild.id] = after.channel.id
        return
    if before.channel and after.channel is None:
        if is_intentional_voice_disconnect_active(guild.id):
            log_voice_event("intentional_disconnect_skip_recovery", guild.id, channel_id=before.channel.id)
            return
        queue = get_guild_queue(guild.id)
        # If playback/queue exists, reconnect and continue automatically.
        if guild.id in current_track or not queue.empty():
            await asyncio.sleep(2)
            vc = await ensure_voice_connection(guild)
            if vc and not vc.is_playing() and not vc.is_paused():
                track = current_track.get(guild.id)
                if track:
                    attempts = track_recovery_attempts.get(guild.id, 0)
                    if attempts < MAX_TRACK_RECOVERY_ATTEMPTS:
                        track_recovery_attempts[guild.id] = attempts + 1
                        ok = await start_track(guild, track, announce=False)
                        if ok:
                            return
                await play_next(guild)

@bot.command(name='join')
async def join(ctx):
    """Join the voice channel that the user is currently in."""
    if not ctx.author.voice:
        await ctx.send("❗ Előbb csatlakozz egy hangcsatornához!")
        return

    channel = ctx.author.voice.channel
    vc, error_code = await connect_voice_with_retries(ctx.guild, channel, reason="join_prefix")
    if not vc:
        await ctx.send(voice_error_message(error_code))
        return

    await ctx.send(f"✅ Csatlakoztam a(z) **{channel.name}** csatornához!")


@bot.command(name='leave')
async def leave(ctx):
    """Disconnect from the current voice channel."""
    if ctx.voice_client:
        guild_id = ctx.guild.id
        mark_intentional_voice_disconnect(guild_id)
        queue = get_guild_queue(guild_id)
        while not queue.empty():
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        if ctx.voice_client.is_playing() or ctx.voice_client.is_paused():
            ctx.voice_client.stop()
        now_playing.pop(guild_id, None)
        current_track.pop(guild_id, None)
        track_recovery_attempts.pop(guild_id, None)
        last_voice_channel_id.pop(guild_id, None)
        await retire_player_message(guild_id)
        await ctx.voice_client.disconnect(force=True)
        await ctx.send("👋 Kiléptem a voice csatornából.")
    else:
        await ctx.send("ℹ️ Nem vagyok voice csatornában.")


@bot.command(name='play')
async def play(ctx, *, query: str):
    """Play a song from YouTube or process Spotify URLs. Add to queue if already playing."""
    vc = ctx.voice_client
    if not vc:
        if not ctx.author.voice:
            await ctx.send("❗ Előbb csatlakozz egy hangcsatornához!")
            return
        channel = ctx.author.voice.channel
        vc, error_code = await connect_voice_with_retries(ctx.guild, channel, reason="play_prefix")
        if not vc:
            await ctx.send(voice_error_message(error_code))
            return

    queue = get_guild_queue(ctx.guild.id)
    if vc and vc.channel:
        last_voice_channel_id[ctx.guild.id] = vc.channel.id

    added_titles: List[str] = []
    if is_youtube_playlist_url(query):
        await ctx.send("📃 YouTube lejátszási lista felismerve, beolvasás...")
        entries, playlist_error = await get_youtube_playlist(query)
        if playlist_error:
            await ctx.send(YOUTUBE_PLAYLIST_ERRORS[playlist_error])
            return
        for entry in entries:
            await queue.put(
                QueuedTrack(source=entry.url, title=entry.title, target=ctx, candidates=[entry])
            )
            added_titles.append(entry.title)
    elif is_spotify_url(query) and SPOTIFY_CLIENT:
        await ctx.send("🎧 Spotify link felismerve, számok hozzáadása...")
        track_terms, spotify_error = await get_spotify_tracks(query)
        if spotify_error:
            await ctx.send(spotify_error_message(spotify_error))
            return
        # Queue the search terms as-is: resolving every track up front made a
        # long playlist take minutes before the first note played.
        for term in track_terms:
            await queue.put(QueuedTrack(source=term, title=term, target=ctx))
            added_titles.append(term)
    else:
        await ctx.send(f"🔎 Keresés: {query}")
        candidates = await search_youtube(query)
        if not candidates:
            await ctx.send("❌ Nem találtam eredményt.")
            return
        if candidates[0].score < SEARCH_CONFIDENT_SCORE:
            # Something the user typed is missing from the best title; let them pick.
            await send_track_chooser(ctx.guild, query, candidates, ctx.author.id, ctx)
            return
        top = candidates[0]
        await queue.put(
            QueuedTrack(source=top.url, title=top.title, target=ctx, candidates=candidates)
        )
        added_titles.append(top.title)

    if added_titles:
        if len(added_titles) == 1:
            await ctx.send(f"✅ Hozzáadva: **{added_titles[0]}**")
        else:
            await ctx.send(f"✅ {len(added_titles)} szám hozzáadva a várólistához.")
            for t in added_titles[:5]:
                await ctx.send(f"+ {t}")
            if len(added_titles) > 5:
                await ctx.send(f"...és {len(added_titles) - 5} további.")

    if vc and not vc.is_playing() and not vc.is_paused():
        await play_next(ctx.guild)

async def play_next(guild: discord.Guild):
    """Play the next song in the queue for a guild."""
    play_lock = get_playback_lock(guild.id)
    async with play_lock:
        if is_intentional_voice_disconnect_active(guild.id):
            return
        queue = get_guild_queue(guild.id)
        if queue.empty():
            vc = guild.voice_client
            if vc and (vc.is_connected() or vc.is_playing() or vc.is_paused()):
                mark_intentional_voice_disconnect(guild.id)
                await vc.disconnect(force=True)
            current_track.pop(guild.id, None)
            now_playing.pop(guild.id, None)
            track_recovery_attempts.pop(guild.id, None)
            await retire_player_message(guild.id)
            return
        vc = await ensure_voice_connection(guild)
        if not vc:
            current_track.pop(guild.id, None)
            now_playing.pop(guild.id, None)
            track_recovery_attempts.pop(guild.id, None)
            return
        if vc.is_playing() or vc.is_paused():
            return
        try:
            track = queue.get_nowait()
        except asyncio.QueueEmpty:
            mark_intentional_voice_disconnect(guild.id)
            current_track.pop(guild.id, None)
            now_playing.pop(guild.id, None)
            track_recovery_attempts.pop(guild.id, None)
            await vc.disconnect(force=True)
            return
        ok = await start_track(guild, track)
        if not ok:
            track.attempts += 1
            if track.attempts >= MAX_TRACK_START_ATTEMPTS:
                # Drop it instead of re-queueing forever; an unplayable track would
                # otherwise loop through the queue indefinitely.
                logger.warning(
                    "Szam eldobva %d sikertelen inditas utan source=%r",
                    track.attempts, track.source,
                )
                await safe_send(track.target, f"⚠️ Nem sikerült lejátszani: **{track.title}** — kihagyom.")
                bot.loop.create_task(retry_play_next_later(guild, 1.0))
            else:
                await queue.put(track)
                bot.loop.create_task(retry_play_next_later(guild, 2.0))


@bot.command(name='skip')
async def skip(ctx):
    """Skip the currently playing song."""
    if ctx.voice_client and ctx.voice_client.is_playing():
        ctx.voice_client.stop()
        await ctx.send("⏭️ Kihagyva az aktuális számot.")
    else:
        await ctx.send("ℹ️ Nem játszik semmi.")


@bot.command(name='pause')
async def pause(ctx):
    """Pause the currently playing song."""
    if ctx.voice_client and ctx.voice_client.is_playing():
        ctx.voice_client.pause()
        await ctx.send("⏸️ Lejátszás szüneteltetve.")
    else:
        await ctx.send("ℹ️ Nem játszik semmi.")


@bot.command(name='resume')
async def resume(ctx):
    """Resume a paused song."""
    if ctx.voice_client and ctx.voice_client.is_paused():
        ctx.voice_client.resume()
        await ctx.send("▶️ Lejátszás folytatva.")
    else:
        await ctx.send("ℹ️ Nem volt szüneteltetve.")


@bot.command(name='np')
async def nowplaying(ctx):
    """Show what is currently playing."""
    title = now_playing.get(ctx.guild.id)
    if title:
        await ctx.send(f"🎶 Most játszom: **{title}**")
    else:
        await ctx.send("ℹ️ Nem játszik semmi.")


@bot.command(name='queue')
async def queue_cmd(ctx):
    """Display the upcoming songs in the queue."""
    queue = get_guild_queue(ctx.guild.id)
    if queue.empty():
        await ctx.send("ℹ️ A várólista üres.")
        return
    # list items without removing them
    titles = queued_titles(ctx.guild.id)
    msg_lines = [f"Várólista ({len(titles)} szám):"]
    for idx, title in enumerate(titles, start=1):
        if idx > 10:
            msg_lines.append(f"…és még {len(titles) - 10} további.")
            break
        msg_lines.append(f"{idx}. {title}")
    await ctx.send("\n".join(msg_lines))


@bot.command(name='stop')
async def stop_cmd(ctx):
    """Stop playback and clear the queue."""
    queue = get_guild_queue(ctx.guild.id)
    # Clear queue
    while not queue.empty():
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            break
    # Stop current playback
    if ctx.voice_client and (ctx.voice_client.is_playing() or ctx.voice_client.is_paused()):
        ctx.voice_client.stop()
    now_playing.pop(ctx.guild.id, None)
    current_track.pop(ctx.guild.id, None)
    track_recovery_attempts.pop(ctx.guild.id, None)
    await retire_player_message(ctx.guild.id)
    await ctx.send("⏹️ Lejátszás leállítva és várólista törölve.")


@bot.command(name='info')
async def info_cmd(ctx):
    """Explain every command and button."""
    embed = build_info_embed()
    if not await safe_send(ctx, embed=embed):
        # No Embed Links permission here; a bare pointer beats silence.
        await safe_send(ctx, f"ℹ️ A súgóhoz *Embed Links* jog kell. Parancsok: `{PREFIX}play`, "
                             f"`{PREFIX}skip`, `{PREFIX}pause`, `{PREFIX}resume`, `{PREFIX}queue`, "
                             f"`{PREFIX}shuffle`, `{PREFIX}stop`, `{PREFIX}join`, `{PREFIX}leave`.")


@bot.command(name='shuffle')
async def shuffle_cmd(ctx):
    """Shuffle the current queue."""
    queue = get_guild_queue(ctx.guild.id)
    if queue.empty():
        await ctx.send("ℹ️ A várólista üres, nincs mit keverni.")
        return
    # Extract all items
    items = []
    while not queue.empty():
        items.append(await queue.get())
    # Shuffle
    random.shuffle(items)
    # Put back
    for item in items:
        await queue.put(item)
    await ctx.send("🔀 A várólista megkeverve.")


if not TOKEN:
    logger.warning("DISCORD_TOKEN nincs beallitva; run_bot inditaskor kotelezo.")

# ---------------------------------------------------------------------------
# Slash command definitions
# ---------------------------------------------------------------------------

music_group = app_commands.Group(name="zene", description="Zenelejátszó parancsok")


@music_group.command(name="join", description="Csatlakozik ahhoz a hangcsatornához, ahol a felhasználó van.")
async def join_slash(interaction: discord.Interaction):
    if not interaction.user.voice:
        await interaction.response.send_message("❗ Előbb csatlakozz egy hangcsatornához!")
        return

    channel = interaction.user.voice.channel  # type: ignore[assignment]
    vc, error_code = await connect_voice_with_retries(interaction.guild, channel, reason="join_slash")
    if not vc:
        await interaction.response.send_message(voice_error_message(error_code))
        return

    await interaction.response.send_message(f"✅ Csatlakoztam a(z) **{channel.name}** csatornához!")


@music_group.command(name="leave", description="Kilép abból a hangcsatornából, amiben a bot van.")
async def leave_slash(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc:
        guild_id = interaction.guild.id
        mark_intentional_voice_disconnect(guild_id)
        queue = get_guild_queue(guild_id)
        while not queue.empty():
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        if vc.is_playing() or vc.is_paused():
            vc.stop()
        now_playing.pop(guild_id, None)
        current_track.pop(guild_id, None)
        track_recovery_attempts.pop(guild_id, None)
        last_voice_channel_id.pop(guild_id, None)
        await retire_player_message(guild_id)
        await vc.disconnect(force=True)
        await interaction.response.send_message("👋 Kiléptem a voice csatornából.")
    else:
        await interaction.response.send_message("ℹ️ Nem vagyok voice csatornában.")


@music_group.command(name="play", description="Lejátszik egy dalt YouTube-ról vagy Spotify hivatkozásról.")
@app_commands.describe(query="Dal címe, YouTube vagy Spotify URL")
@app_commands.autocomplete(query=yt_autocomplete)
async def play_slash(interaction: discord.Interaction, query: str):
    await interaction.response.defer()
    vc = interaction.guild.voice_client
    if vc and vc.channel:
        last_voice_channel_id[interaction.guild.id] = vc.channel.id

    if not vc:
        if not interaction.user.voice:
            await interaction.followup.send("❗ Előbb csatlakozz egy hangcsatornához!")
            return

        channel = interaction.user.voice.channel  # type: ignore[assignment]
        vc, error_code = await connect_voice_with_retries(interaction.guild, channel, reason="play_slash")
        if not vc:
            await interaction.followup.send(voice_error_message(error_code))
            return

    queue = get_guild_queue(interaction.guild.id)
    added_titles: List[str] = []

    if is_youtube_playlist_url(query):
        await interaction.followup.send("📃 YouTube lejátszási lista felismerve, beolvasás...")
        entries, playlist_error = await get_youtube_playlist(query)
        if playlist_error:
            await interaction.followup.send(YOUTUBE_PLAYLIST_ERRORS[playlist_error])
            return
        for entry in entries:
            await queue.put(QueuedTrack(
                source=entry.url, title=entry.title, target=interaction, candidates=[entry]
            ))
            added_titles.append(entry.title)
    elif is_spotify_url(query) and SPOTIFY_CLIENT:
        await interaction.followup.send("🎧 Spotify link felismerve, számok hozzáadása...")
        track_terms, spotify_error = await get_spotify_tracks(query)
        if spotify_error:
            await interaction.followup.send(spotify_error_message(spotify_error))
            return
        # Queue the search terms as-is: resolving every track up front made a
        # long playlist take minutes before the first note played.
        for term in track_terms:
            await queue.put(QueuedTrack(source=term, title=term, target=interaction))
            added_titles.append(term)
    else:
        await interaction.followup.send(f"🔎 Keresés: {query}")
        candidates = await search_youtube(query)
        if not candidates:
            await interaction.followup.send("❌ Nem találtam eredményt.")
            return
        if candidates[0].score < SEARCH_CONFIDENT_SCORE:
            # Something the user typed is missing from the best title; let them pick.
            await send_track_chooser(
                interaction.guild, query, candidates, interaction.user.id, interaction
            )
            return
        top = candidates[0]
        await queue.put(
            QueuedTrack(
                source=top.url, title=top.title, target=interaction, candidates=candidates
            )
        )
        added_titles.append(top.title)

    if added_titles:
        if len(added_titles) == 1:
            await interaction.followup.send(f"✅ Hozzáadva: **{added_titles[0]}**")
        else:
            await interaction.followup.send(f"✅ {len(added_titles)} szám hozzáadva a várólistához.")
            for t in added_titles[:5]:
                await interaction.followup.send(f"+ {t}")
            if len(added_titles) > 5:
                await interaction.followup.send(f"...és {len(added_titles) - 5} további.")

    if vc and not vc.is_playing() and not vc.is_paused():
        await play_next(interaction.guild)


@music_group.command(name="info", description="Elmagyarázza az összes parancsot és gombot.")
async def info_slash(interaction: discord.Interaction):
    await interaction.response.send_message(embed=build_info_embed())


@music_group.command(name="skip", description="Kihagyja az aktuálisan játszott számot.")
async def skip_slash(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_playing():
        vc.stop()
        await interaction.response.send_message("⏭️ Kihagyva az aktuális számot.")
    else:
        await interaction.response.send_message("ℹ️ Nem játszik semmi.")


@music_group.command(name="pause", description="Szünetelteti az aktuális lejátszást.")
async def pause_slash(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_playing():
        vc.pause()
        await interaction.response.send_message("⏸️ Lejátszás szüneteltetve.")
    else:
        await interaction.response.send_message("ℹ️ Nem játszik semmi.")


@music_group.command(name="resume", description="Folytatja a szüneteltetett lejátszást.")
async def resume_slash(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_paused():
        vc.resume()
        await interaction.response.send_message("▶️ Lejátszás folytatva.")
    else:
        await interaction.response.send_message("ℹ️ Nem volt szüneteltetve.")


@music_group.command(name="np", description="Megjeleníti az aktuális számot.")
async def now_playing_slash(interaction: discord.Interaction):
    title = now_playing.get(interaction.guild.id)
    if title:
        await interaction.response.send_message(f"🎶 Most játszom: **{title}**")
    else:
        await interaction.response.send_message("ℹ️ Nem játszik semmi.")


@music_group.command(name="queue", description="Megjeleníti a várólistában lévő számokat.")
async def queue_slash(interaction: discord.Interaction):
    q = get_guild_queue(interaction.guild.id)
    if q.empty():
        await interaction.response.send_message("ℹ️ A várólista üres.")
        return

    titles = queued_titles(interaction.guild.id)
    lines = [f"📋 Várólista ({len(titles)} szám):"]
    for idx, title in enumerate(titles, start=1):
        if idx > 10:
            lines.append(f"...és még {len(titles) - 10} további.")
            break
        lines.append(f"{idx}. {title}")
    await interaction.response.send_message("\n".join(lines))


@music_group.command(name="stop", description="Leállítja a lejátszást és törli a várólistát.")
async def stop_slash(interaction: discord.Interaction):
    q = get_guild_queue(interaction.guild.id)
    while not q.empty():
        try:
            q.get_nowait()
        except asyncio.QueueEmpty:
            break

    vc = interaction.guild.voice_client
    if vc and (vc.is_playing() or vc.is_paused()):
        vc.stop()
    now_playing.pop(interaction.guild.id, None)
    current_track.pop(interaction.guild.id, None)
    track_recovery_attempts.pop(interaction.guild.id, None)
    await retire_player_message(interaction.guild.id)
    await interaction.response.send_message("⏹️ Lejátszás leállítva és várólista törölve.")


@music_group.command(name="shuffle", description="Megkeveri a várólistát.")
async def shuffle_slash(interaction: discord.Interaction):
    q = get_guild_queue(interaction.guild.id)
    if q.empty():
        await interaction.response.send_message("ℹ️ A várólista üres, nincs mit keverni.")
        return

    items = []
    while not q.empty():
        items.append(await q.get())
    random.shuffle(items)
    for item in items:
        await q.put(item)
    await interaction.response.send_message("🔀 A várólista megkeverve.")


bot.tree.add_command(music_group)


def run_bot() -> None:
    """Entrypoint for starting the Discord bot."""
    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN nincs beallitva a kornyezetben.")
    bot.run(TOKEN)


if __name__ == "__main__":
    run_bot()



