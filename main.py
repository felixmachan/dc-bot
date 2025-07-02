import discord
from discord.ext import commands
import asyncio
import yt_dlp

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

# Queue kezelése
song_queue = {}
now_playing = {}

def get_guild_queue(guild_id):
    if guild_id not in song_queue:
        song_queue[guild_id] = asyncio.Queue()
    return song_queue[guild_id]

@bot.event
async def on_ready():
    print(f'✅ Bot elindult: {bot.user}')

# Csatlakozás voice csatornához
@bot.command()
async def join(ctx):
    if ctx.author.voice:
        channel = ctx.author.voice.channel
        if ctx.voice_client is None:
            await channel.connect()
        else:
            await ctx.voice_client.move_to(channel)
        await ctx.send(f"🔊 Csatlakoztam: {channel.name}")
    else:
        await ctx.send("Előbb csatlakozz egy hangcsatornához!")

# Kilépés voice csatornából
@bot.command()
async def leave(ctx):
    if ctx.voice_client:
        await ctx.voice_client.disconnect()
        await ctx.send("👋 Kiléptem.")
    else:
        await ctx.send("Nem vagyok voice csatornában.")

# Lejátszás vagy queue-ba rakás
@bot.command()
async def play(ctx, *, query):
    vc = ctx.voice_client
    if not vc:
        await ctx.invoke(join)
        vc = ctx.voice_client

    queue = get_guild_queue(ctx.guild.id)

    ydl_opts = {
        'format': 'bestaudio/best',
        'quiet': True,
        'default_search': 'ytsearch',
        'extract_flat': False, 
    }

    await ctx.send(f"🔍 Keresés: {query}")

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(query, download=False)

    entries = []
    if 'entries' in info:  # Playlist
        entries = info['entries']
    else:  # Egyetlen videó
        entries = [info]

    added_titles = []
    for entry in entries:
        url = entry['url']
        title = entry.get('title', 'Ismeretlen')
        await queue.put((url, title, ctx))
        added_titles.append(title)

    if len(added_titles) == 1:
        await ctx.send(f"🎶 Hozzáadva: **{added_titles[0]}**")
    else:
        await ctx.send(f"📜 {len(added_titles)} szám hozzáadva a várólistához.")
        for title in added_titles[:5]:
            await ctx.send(f"➕ {title}")
        if len(added_titles) > 5:
            await ctx.send(f"…és {len(added_titles)-5} további.")

    if not vc.is_playing():
        await play_next(ctx.guild)

async def play_next(guild):
    vc = guild.voice_client
    queue = get_guild_queue(guild.id)

    if queue.empty():
        await vc.disconnect()
        return

    url, title, ctx = await queue.get()
    source = await discord.FFmpegOpusAudio.from_probe(url)

    def after(e):
        fut = play_next(guild)
        fut = asyncio.run_coroutine_threadsafe(fut, bot.loop)
        try:
            fut.result()
        except Exception as exc:
            print(f"Hiba a következő szám lejátszásánál: {exc}")

    now_playing[guild.id] = title
    vc.play(source, after=after)
    await ctx.send(f"🎧 Most játszom: **{title}**")

@bot.command()
async def skip(ctx):
    if ctx.voice_client and ctx.voice_client.is_playing():
        ctx.voice_client.stop()
        await ctx.send("⏭️ Kihagyva az aktuális szám.")
    else:
        await ctx.send("Nem játszik semmi.")

@bot.command()
async def pause(ctx):
    if ctx.voice_client and ctx.voice_client.is_playing():
        ctx.voice_client.pause()
        await ctx.send("⏸️ Lejátszás szüneteltetve.")
    else:
        await ctx.send("Nem játszik semmi.")

@bot.command()
async def resume(ctx):
    if ctx.voice_client and ctx.voice_client.is_paused():
        ctx.voice_client.resume()
        await ctx.send("▶️ Lejátszás folytatva.")
    else:
        await ctx.send("Nem volt szüneteltetve.")

@bot.command()
async def np(ctx):
    title = now_playing.get(ctx.guild.id, None)
    if title:
        await ctx.send(f"🎶 Most játszom: **{title}**")
    else:
        await ctx.send("Nem játszik semmi.")

bot.run("IDE_JÖN_A_DISCORD_BOT_TOKEN")
