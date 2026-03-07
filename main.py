import os
import re
import asyncio
import random
import glob
import shutil
import time
import logging
from typing import List, Tuple, Optional, Iterable

import discord
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv

import yt_dlp

try:
    import spotipy
    from spotipy.oauth2 import SpotifyClientCredentials
except ImportError:
    # spotipy is optional; the bot will still work without Spotify support
    spotipy = None
    SpotifyClientCredentials = None


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

LOG_LEVEL = parse_log_level()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("dc_bot")

# Spotify credentials (optional)
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix=PREFIX, intents=intents)
last_resync_ts: float = 0.0

# Per‑guild song queues and currently playing information
# Each item in the queue is a tuple of (audio_url, title, target)
song_queue: dict[int, asyncio.Queue] = {}
now_playing: dict[int, str] = {}
current_track: dict[int, Tuple[str, str, object]] = {}
playback_locks: dict[int, asyncio.Lock] = {}
voice_reconnect_locks: dict[int, asyncio.Lock] = {}
last_voice_channel_id: dict[int, int] = {}
track_recovery_attempts: dict[int, int] = {}
MAX_TRACK_RECOVERY_ATTEMPTS = 2
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


def is_spotify_url(url: str) -> bool:
    """Quick check whether a URL points to Spotify content."""
    return 'open.spotify.com' in url


def create_spotify_client() -> Optional[spotipy.Spotify]:
    """Create a Spotify client if credentials are available and spotipy is installed."""
    if not spotipy or not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
        return None
    auth_manager = SpotifyClientCredentials(client_id=SPOTIFY_CLIENT_ID, client_secret=SPOTIFY_CLIENT_SECRET)
    return spotipy.Spotify(auth_manager=auth_manager)


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


async def search_youtube(term: str) -> Optional[Tuple[str, str]]:
    """Search YouTube using yt_dlp and return the first audio result (url, title)."""
    ydl_opts = {
        'format': 'bestaudio[ext=m4a]/bestaudio[acodec!=none]/best',
        'quiet': True,
        'noplaylist': True,
        'extract_flat': False,
        'skip_download': True,
        # Prefer clients that usually expose direct media URLs over SABR-limited web formats.
        'extractor_args': {
            'youtube': {
                'player_client': ['android', 'web'],
            }
        },
    }
    search_term = term if is_url(term) else f"ytsearch:{term}"
    def _extract() -> object:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(search_term, download=False)

    try:
        info = await asyncio.wait_for(asyncio.to_thread(_extract), timeout=15.0)
    except asyncio.TimeoutError:
        logger.warning("yt-dlp search timeout term=%r", term)
        return None
    except Exception as e:
        logger.error("yt-dlp search error term=%r error=%s", term, e)
        return None
    # Normalise entries
    entries = info.get('entries', [info])
    if not entries:
        return None
    entry = entries[0]
    # Let yt-dlp provide the final selected URL first; manual format fallback only if needed.
    audio_url = entry.get('url')
    if not audio_url:
        formats = entry.get('formats', [])
        audio_formats = [
            f for f in formats
            if f.get('acodec') != 'none'
            and f.get('vcodec') == 'none'
            and f.get('url')
            and f.get('protocol') not in {'m3u8', 'http_dash_segments'}
        ]
        if audio_formats:
            audio_formats.sort(key=lambda f: f.get('abr') or f.get('asr') or 0, reverse=True)
            audio_url = audio_formats[0]['url']
        else:
            return None
    title = entry.get('title', 'Ismeretlen')
    return audio_url, title


def parse_spotify_id(url: str) -> Optional[Tuple[str, str]]:
    """Extract Spotify content type and ID from a URL."""
    # Match patterns like /track/{id}, /playlist/{id}, /album/{id}
    match = re.search(r'open\.spotify\.com/(track|playlist|album)/([A-Za-z0-9]+)', url)
    if not match:
        return None
    return match.group(1), match.group(2)


async def get_spotify_tracks(url: str) -> List[str]:
    """Given a Spotify URL, return a list of track names with artist for searching."""
    result: List[str] = []
    if not SPOTIFY_CLIENT:
        return result
    parsed = parse_spotify_id(url)
    if not parsed:
        return result
    content_type, spotify_id = parsed
    def _fetch_tracks() -> List[str]:
        local_result: List[str] = []
        if content_type == 'track':
            track = SPOTIFY_CLIENT.track(spotify_id)
            name = track['name']
            artists = ', '.join(artist['name'] for artist in track['artists'])
            local_result.append(f"{name} {artists}")
        elif content_type == 'album':
            album_tracks = SPOTIFY_CLIENT.album_tracks(spotify_id)
            for item in album_tracks['items']:
                name = item['name']
                artists = ', '.join(artist['name'] for artist in item['artists'])
                local_result.append(f"{name} {artists}")
        elif content_type == 'playlist':
            # limit to first 50 tracks to prevent huge queues
            playlist_tracks = SPOTIFY_CLIENT.playlist_items(
                spotify_id,
                fields='items(track(name,artists(name)))',
                additional_types=['track'],
                limit=50
            )
            for item in playlist_tracks['items']:
                track = item['track']
                if track:
                    name = track['name']
                    artists = ', '.join(a['name'] for a in track['artists'])
                    local_result.append(f"{name} {artists}")
        return local_result

    try:
        result = await asyncio.wait_for(asyncio.to_thread(_fetch_tracks), timeout=15.0)
    except Exception as e:
        logger.warning("Spotify fetch failed error=%s", e)
    return result


@bot.event
async def on_ready():
    logger.info("Bot elindult user=%s", bot.user)
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


async def safe_send(target: object, message: str):
    """Send a message to either Context or Interaction target safely."""
    try:
        await target.send(message)  # type: ignore[attr-defined]
    except AttributeError:
        await target.followup.send(message)  # type: ignore[attr-defined]
    except Exception as send_err:
        logger.warning("safe_send failed error=%s", send_err)


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



async def start_track(guild: discord.Guild, url: str, title: str, target: object, announce: bool = True) -> bool:
    """Start playing one track and wire an error-tolerant after callback."""
    vc = await ensure_voice_connection(guild, target)
    if not vc:
        return False
    if not FFMPEG_EXE:
        logger.error("FFmpeg nincs telepitve vagy nem talalhato.")
        return False

    source = discord.FFmpegPCMAudio(
        url,
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

    now_playing[guild.id] = title
    current_track[guild.id] = (url, title, target)
    if announce:
        await safe_send(target, f"🎶 Most játszom: **{title}**")
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
                ok = await start_track(guild, track[0], track[1], track[2], announce=False)
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
                        ok = await start_track(guild, track[0], track[1], track[2], announce=False)
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
    if is_spotify_url(query) and SPOTIFY_CLIENT:
        await ctx.send("🎧 Spotify link felismerve, számok hozzáadása...")
        track_terms = await get_spotify_tracks(query)
        if not track_terms:
            await ctx.send("❌ Nem sikerült beolvasni a Spotify tartalmat, vagy üres a lejátszási lista.")
        for term in track_terms:
            res = await search_youtube(term)
            if res:
                audio_url, title = res
                await queue.put((audio_url, title, ctx))
                added_titles.append(title)
    else:
        await ctx.send(f"🔎 Keresés: {query}")
        res = await search_youtube(query)
        if res:
            audio_url, title = res
            await queue.put((audio_url, title, ctx))
            added_titles.append(title)
        else:
            await ctx.send("❌ Nem találtam eredményt.")
            return

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
            url, title, target = queue.get_nowait()
        except asyncio.QueueEmpty:
            mark_intentional_voice_disconnect(guild.id)
            current_track.pop(guild.id, None)
            now_playing.pop(guild.id, None)
            track_recovery_attempts.pop(guild.id, None)
            await vc.disconnect(force=True)
            return
        ok = await start_track(guild, url, title, target)
        if not ok:
            await queue.put((url, title, target))
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
    items = list(queue._queue)  # type: ignore[attr-defined]
    msg_lines = [f"Várólista ({len(items)} szám):"]
    for idx, (_, title, _) in enumerate(items, start=1):
        if idx > 10:
            msg_lines.append(f"…és még {len(items) - 10} további.")
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
    await ctx.send("⏹️ Lejátszás leállítva és várólista törölve.")


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

    if is_spotify_url(query) and SPOTIFY_CLIENT:
        await interaction.followup.send("🎧 Spotify link felismerve, számok hozzáadása...")
        track_terms = await get_spotify_tracks(query)
        if not track_terms:
            await interaction.followup.send("❌ Nem sikerült beolvasni a Spotify tartalmat, vagy üres a lejátszási lista.")
        for term in track_terms:
            res = await search_youtube(term)
            if res:
                audio_url, title = res
                await queue.put((audio_url, title, interaction))
                added_titles.append(title)
    else:
        await interaction.followup.send(f"🔎 Keresés: {query}")
        res = await search_youtube(query)
        if res:
            audio_url, title = res
            await queue.put((audio_url, title, interaction))
            added_titles.append(title)
        else:
            await interaction.followup.send("❌ Nem találtam eredményt.")
            return

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

    items: Iterable[Tuple[str, str, object]] = list(q._queue)  # type: ignore[attr-defined]
    lines = [f"📋 Várólista ({len(items)} szám):"]
    for idx, (_, title, _) in enumerate(items, start=1):
        if idx > 10:
            lines.append(f"...és még {len(items) - 10} további.")
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



