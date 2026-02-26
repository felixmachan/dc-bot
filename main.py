import os
import re
import asyncio
import random
import glob
import shutil
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
# Bot token and prefix from environment; prefix defaults to '!'
TOKEN = os.getenv("DISCORD_TOKEN")
PREFIX = os.getenv("DISCORD_PREFIX", "!")
GUILD_ID_RAW = os.getenv("DISCORD_GUILD_ID")
GUILD_ID = int(GUILD_ID_RAW) if GUILD_ID_RAW and GUILD_ID_RAW.isdigit() else None

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
    # Don't return suggestions for empty input to avoid spamming the API
    if not current:
        return []
    # Prepare search options: flat extraction to avoid deep info and limit results
    search_opts = {
        'format': 'bestaudio/best',
        'quiet': True,
        'noplaylist': True,
        'extract_flat': True,
        'skip_download': True,
    }
    suggestions: List[app_commands.Choice[str]] = []
    try:
        # Search for up to 5 results using ytsearch5:
        with yt_dlp.YoutubeDL(search_opts) as ydl:
            info = ydl.extract_info(f"ytsearch5:{current}", download=False)
        for entry in info.get('entries', []):
            title = entry.get('title')
            if title:
                suggestions.append(app_commands.Choice(name=title, value=title))
            if len(suggestions) >= 5:
                break
    except Exception:
        # On error just return an empty list (no suggestions)
        return []
    return suggestions


async def search_youtube(term: str) -> Optional[Tuple[str, str]]:
    """Search YouTube using yt_dlp and return the first audio result (url, title)."""
    ydl_opts = {
        'format': 'bestaudio/best',
        'quiet': True,
        'noplaylist': True,
        'extract_flat': False,
        'skip_download': True,
    }
    search_term = term if is_url(term) else f"ytsearch:{term}"
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(search_term, download=False)
    except Exception as e:
        print(f"yt-dlp search error for term '{term}': {e}")
        return None
    # Normalise entries
    entries = info.get('entries', [info])
    if not entries:
        return None
    entry = entries[0]
    # Determine audio URL
    formats = entry.get('formats', [])
    audio_formats = [f for f in formats if f.get('acodec') != 'none' and f.get('vcodec') == 'none']
    if audio_formats:
        audio_formats.sort(key=lambda f: f.get('abr') or f.get('asr') or 0, reverse=True)
        audio_url = audio_formats[0]['url']
    else:
        audio_url = entry.get('url')
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
    try:
        if content_type == 'track':
            track = SPOTIFY_CLIENT.track(spotify_id)
            name = track['name']
            artists = ', '.join(artist['name'] for artist in track['artists'])
            result.append(f"{name} {artists}")
        elif content_type == 'album':
            album_tracks = SPOTIFY_CLIENT.album_tracks(spotify_id)
            for item in album_tracks['items']:
                name = item['name']
                artists = ', '.join(artist['name'] for artist in item['artists'])
                result.append(f"{name} {artists}")
        elif content_type == 'playlist':
            # limit to first 50 tracks to prevent huge queues
            playlist_tracks = SPOTIFY_CLIENT.playlist_items(spotify_id, fields='items(track(name,artists(name)))', additional_types=['track'], limit=50)
            for item in playlist_tracks['items']:
                track = item['track']
                if track:
                    name = track['name']
                    artists = ', '.join(a['name'] for a in track['artists'])
                    result.append(f"{name} {artists}")
    except Exception as e:
        print(f"Failed to fetch Spotify data: {e}")
    return result


@bot.event
async def on_ready():
    print(f"Bot elindult: {bot.user}")
    try:
        # Keep a global registration for portability across guilds.
        # If a guild ID is configured, sync that too for faster propagation there.
        global_synced = await bot.tree.sync()
        print(f"Global slash sync kesz: {len(global_synced)} parancs.")
        if GUILD_ID:
            guild_obj = discord.Object(id=GUILD_ID)
            guild_synced = await bot.tree.sync(guild=guild_obj)
            print(f"Guild slash sync kesz: {len(guild_synced)} parancs (guild_id={GUILD_ID}).")
    except Exception as e:
        print(f"Slash parancsok szinkronizalasa sikertelen: {e}")


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    global last_resync_ts
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
                print(f"Automatikus slash re-sync sikertelen: {sync_err}")
        msg = "A slash parancsok frissulnek. Probald ujra par masodperc mulva."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
        return
    print(f"App command hiba: {error}")

@bot.command(name='join')
async def join(ctx):
    """Join the voice channel that the user is currently in."""
    if ctx.author.voice:
        channel = ctx.author.voice.channel
        try:
            if ctx.voice_client is None:
                await channel.connect(timeout=10)
            else:
                await ctx.voice_client.move_to(channel)
            await ctx.send(f"🔊 Csatlakoztam a(z) {channel.name} csatornához!")
        except asyncio.TimeoutError:
            await ctx.send("⚠️ Nem sikerült csatlakozni a voice csatornához: timeout.")
        except Exception as e:
            await ctx.send(f"⚠️ Hiba történt a csatlakozás során: {e}")
    else:
        await ctx.send("Előbb csatlakozz egy hangcsatornához!")


@bot.command(name='leave')
async def leave(ctx):
    """Disconnect from the current voice channel."""
    if ctx.voice_client:
        await ctx.voice_client.disconnect()
        await ctx.send("👋 Kiléptem a voice csatornából.")
    else:
        await ctx.send("Nem vagyok voice csatornában.")


@bot.command(name='play')
async def play(ctx, *, query: str):
    """Play a song from YouTube or process Spotify URLs. Add to queue if already playing."""
    vc = ctx.voice_client
    if not vc:
        await ctx.invoke(join)
        vc = ctx.voice_client
        if not vc:
            return
    queue = get_guild_queue(ctx.guild.id)
    added_titles: List[str] = []
    # Determine if query is a Spotify link
    if is_spotify_url(query) and SPOTIFY_CLIENT:
        await ctx.send("🎵 Spotify link felismerve, számok hozzáadása...")
        track_terms = await get_spotify_tracks(query)
        if not track_terms:
            await ctx.send("❌ Nem sikerült beolvasni a Spotify tartalmat vagy üres a lejátszási lista.")
        for term in track_terms:
            res = await search_youtube(term)
            if res:
                audio_url, title = res
                await queue.put((audio_url, title, ctx))
                added_titles.append(title)
    else:
        # treat as a normal query (YouTube URL or search term)
        await ctx.send(f"🔍 Keresés: {query}")
        res = await search_youtube(query)
        if res:
            audio_url, title = res
            await queue.put((audio_url, title, ctx))
            added_titles.append(title)
        else:
            await ctx.send("❌ Nem találtam eredményt.")
            return
    # Send information about added songs
    if added_titles:
        if len(added_titles) == 1:
            await ctx.send(f"🎶 Hozzáadva: **{added_titles[0]}**")
        else:
            await ctx.send(f"📜 {len(added_titles)} szám hozzáadva a várólistához.")
            for t in added_titles[:5]:
                await ctx.send(f"➕ {t}")
            if len(added_titles) > 5:
                await ctx.send(f"…és {len(added_titles) - 5} további.")
    # Start playing if nothing is playing
    if vc and not vc.is_playing() and not vc.is_paused():
        await play_next(ctx.guild)


async def play_next(guild: discord.Guild):
    """Play the next song in the queue for a guild."""
    vc = guild.voice_client
    if not vc or not vc.is_connected():
        return
    queue = get_guild_queue(guild.id)
    if queue.empty():
        await vc.disconnect()
        return
    if not FFMPEG_EXE:
        print("FFmpeg nincs telepitve vagy nem talalhato. Telepitsd es inditsd ujra a botot.")
        return
    url, title, target = await queue.get()
    # Create source using FFmpeg
    source = discord.FFmpegPCMAudio(
        url,
        executable=FFMPEG_EXE,
        before_options="-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -nostdin",
        options="-vn -loglevel panic"
    )
    def after(error):
        if error:
            print(f"Lejátszási hiba: {error}")
        fut = asyncio.run_coroutine_threadsafe(play_next(guild), bot.loop)
        try:
            fut.result()
        except Exception as exc:
            print(f"Hiba a következő szám lejátszásánál: {exc}")
    now_playing[guild.id] = title
    vc.play(source, after=after)
    # Send message about currently playing track. Handle both Context and Interaction targets.
    try:
        # commands.Context or a messageable channel has a send method
        await target.send(f"🎧 Most játszom: **{title}**")
    except AttributeError:
        # Interaction: use followup to send after deferred response
        await target.followup.send(f"🎧 Most játszom: **{title}**")


@bot.command(name='skip')
async def skip(ctx):
    """Skip the currently playing song."""
    if ctx.voice_client and ctx.voice_client.is_playing():
        ctx.voice_client.stop()
        await ctx.send("⏭️ Kihagyva az aktuális számot.")
    else:
        await ctx.send("Nem játszik semmi.")


@bot.command(name='pause')
async def pause(ctx):
    """Pause the currently playing song."""
    if ctx.voice_client and ctx.voice_client.is_playing():
        ctx.voice_client.pause()
        await ctx.send("⏸️ Lejátszás szüneteltetve.")
    else:
        await ctx.send("Nem játszik semmi.")


@bot.command(name='resume')
async def resume(ctx):
    """Resume a paused song."""
    if ctx.voice_client and ctx.voice_client.is_paused():
        ctx.voice_client.resume()
        await ctx.send("▶️ Lejátszás folytatva.")
    else:
        await ctx.send("Nem volt szüneteltetve.")


@bot.command(name='np')
async def nowplaying(ctx):
    """Show what is currently playing."""
    title = now_playing.get(ctx.guild.id)
    if title:
        await ctx.send(f"🎶 Most játszom: **{title}**")
    else:
        await ctx.send("Nem játszik semmi.")


@bot.command(name='queue')
async def queue_cmd(ctx):
    """Display the upcoming songs in the queue."""
    queue = get_guild_queue(ctx.guild.id)
    if queue.empty():
        await ctx.send("A várólista üres.")
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
    await ctx.send("⏹️ Lejátszás leállítva és várólista törölve.")


@bot.command(name='shuffle')
async def shuffle_cmd(ctx):
    """Shuffle the current queue."""
    queue = get_guild_queue(ctx.guild.id)
    if queue.empty():
        await ctx.send("A várólista üres, nincs mit keverni.")
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
    raise RuntimeError("DISCORD_TOKEN nincs beállítva a környezetben.")

# ---------------------------------------------------------------------------
# Slash command definitions
# ---------------------------------------------------------------------------

music_group = app_commands.Group(name="zene", description="Zene lejatszo parancsok")


@music_group.command(name="join", description="Csatlakozik a hangcsatornahoz, ahol a felhasznalo van.")
async def join_slash(interaction: discord.Interaction):
    if interaction.user.voice:
        channel = interaction.user.voice.channel  # type: ignore[assignment]
        try:
            if interaction.guild.voice_client is None:
                await channel.connect(timeout=10)
            else:
                await interaction.guild.voice_client.move_to(channel)  # type: ignore[union-attr]
            await interaction.response.send_message(f"Csatlakoztam a(z) {channel.name} csatornahoz!")
        except asyncio.TimeoutError:
            await interaction.response.send_message("Nem sikerult csatlakozni a voice csatornahoz: timeout.")
        except Exception as e:
            await interaction.response.send_message(f"Hiba tortent a csatlakozas soran: {e}")
    else:
        await interaction.response.send_message("Elobb csatlakozz egy hangcsatornahoz!")


@music_group.command(name="leave", description="Kilep a hangcsatornabol, amiben a bot van.")
async def leave_slash(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc:
        await vc.disconnect()
        await interaction.response.send_message("Kileptem a voice csatornabol.")
    else:
        await interaction.response.send_message("Nem vagyok voice csatornaban.")


@music_group.command(name="play", description="Lejatszik egy dalt YouTube-rol vagy Spotify hivatkozasrol.")
@app_commands.describe(query="Dal cime, YouTube vagy Spotify URL")
@app_commands.autocomplete(query=yt_autocomplete)
async def play_slash(interaction: discord.Interaction, query: str):
    await interaction.response.defer()
    vc = interaction.guild.voice_client
    if not vc:
        if interaction.user.voice:
            channel = interaction.user.voice.channel  # type: ignore[assignment]
            try:
                await channel.connect(timeout=10)
            except asyncio.TimeoutError:
                await interaction.followup.send("Nem sikerult csatlakozni a voice csatornahoz: timeout.")
                return
            except Exception as e:
                await interaction.followup.send(f"Hiba tortent a csatlakozas soran: {e}")
                return
            vc = interaction.guild.voice_client
        else:
            await interaction.followup.send("Elobb csatlakozz egy hangcsatornahoz!")
            return

    queue = get_guild_queue(interaction.guild.id)
    added_titles: List[str] = []

    if is_spotify_url(query) and SPOTIFY_CLIENT:
        await interaction.followup.send("Spotify link felismerve, szamok hozzaadasa...")
        track_terms = await get_spotify_tracks(query)
        if not track_terms:
            await interaction.followup.send("Nem sikerult beolvasni a Spotify tartalmat vagy ures a lejatszasi lista.")
        for term in track_terms:
            res = await search_youtube(term)
            if res:
                audio_url, title = res
                await queue.put((audio_url, title, interaction))
                added_titles.append(title)
    else:
        await interaction.followup.send(f"Kereses: {query}")
        res = await search_youtube(query)
        if res:
            audio_url, title = res
            await queue.put((audio_url, title, interaction))
            added_titles.append(title)
        else:
            await interaction.followup.send("Nem talaltam eredmenyt.")
            return

    if added_titles:
        if len(added_titles) == 1:
            await interaction.followup.send(f"Hozzaadva: **{added_titles[0]}**")
        else:
            await interaction.followup.send(f"{len(added_titles)} szam hozzaadva a varolistahoz.")
            for t in added_titles[:5]:
                await interaction.followup.send(f"+ {t}")
            if len(added_titles) > 5:
                await interaction.followup.send(f"...es {len(added_titles) - 5} tovabbi.")

    if vc and not vc.is_playing() and not vc.is_paused():
        await play_next(interaction.guild)


@music_group.command(name="skip", description="Kihagyja az aktualisan jatszott szamot.")
async def skip_slash(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_playing():
        vc.stop()
        await interaction.response.send_message("Kihagyva az aktualis szamot.")
    else:
        await interaction.response.send_message("Nem jatszik semmi.")


@music_group.command(name="pause", description="Szunetelteti az aktualis lejatszast.")
async def pause_slash(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_playing():
        vc.pause()
        await interaction.response.send_message("Lejatszas szuneteltetve.")
    else:
        await interaction.response.send_message("Nem jatszik semmi.")


@music_group.command(name="resume", description="Folytatja a szuneteltetett lejatszast.")
async def resume_slash(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_paused():
        vc.resume()
        await interaction.response.send_message("Lejatszas folytatva.")
    else:
        await interaction.response.send_message("Nem volt szuneteltetve.")


@music_group.command(name="np", description="Megjeleniti az aktualis szamot.")
async def now_playing_slash(interaction: discord.Interaction):
    title = now_playing.get(interaction.guild.id)
    if title:
        await interaction.response.send_message(f"Most jatszom: **{title}**")
    else:
        await interaction.response.send_message("Nem jatszik semmi.")


@music_group.command(name="queue", description="Megjeleniti a varolistaban levo szamokat.")
async def queue_slash(interaction: discord.Interaction):
    q = get_guild_queue(interaction.guild.id)
    if q.empty():
        await interaction.response.send_message("A varolista ures.")
        return

    items: Iterable[Tuple[str, str, object]] = list(q._queue)  # type: ignore[attr-defined]
    lines = [f"Varolista ({len(items)} szam):"]
    for idx, (_, title, _) in enumerate(items, start=1):
        if idx > 10:
            lines.append(f"...es meg {len(items) - 10} tovabbi.")
            break
        lines.append(f"{idx}. {title}")
    await interaction.response.send_message("\n".join(lines))


@music_group.command(name="stop", description="Leallitja a lejatszast es torli a varolistat.")
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
    await interaction.response.send_message("Lejatszas leallitva es varolista torolve.")


@music_group.command(name="shuffle", description="Megkeveri a varolistat.")
async def shuffle_slash(interaction: discord.Interaction):
    q = get_guild_queue(interaction.guild.id)
    if q.empty():
        await interaction.response.send_message("A varolista ures, nincs mit keverni.")
        return

    items = []
    while not q.empty():
        items.append(await q.get())
    random.shuffle(items)
    for item in items:
        await q.put(item)
    await interaction.response.send_message("A varolista megkeverve.")


bot.tree.add_command(music_group)

# Start the bot after registering all commands
bot.run(TOKEN)

