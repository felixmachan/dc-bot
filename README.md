# 🎵 Discord Music Bot

A simple yet functional Discord music bot built with Python, [discord.py](https://discordpy.readthedocs.io/), and [yt-dlp](https://github.com/yt-dlp/yt-dlp).  
Supports YouTube streaming with a queue, pause/resume, skip, and current playing info.

---

## ⚙️ Features

- ✅ Join and leave voice channels
- 🔍 Search for music on YouTube
- 🎧 Stream music (no download required)
- ➕ Queue management (add multiple songs)
- ⏸️ Pause / ▶️ Resume / ⏭️ Skip songs
- 📝 Display the currently playing track

---

## 📦 Requirements

- Python 3.9+
- FFmpeg (must be in PATH if on Windows)
- Recommended: Virtual environment
- A server if you want to host it 24/7

---

## 🧪 Setup

0. **Install required packages**

- Linux:

```bash
sudo apt update
sudo apt install -y python3 python3-pip ffmpeg
```

- Windows:
  - [Python](https://www.python.org/)
  - [FFMPEG](https://github.com/BtbN/FFmpeg-Builds/releases)
  - Add `C:\Program Files\ffmpeg\bin` to your PATH

1. **Clone the repository**

   ```bash
   git clone https://github.com/yourname/discord-music-bot.git
   cd discord-music-bot
   ```

2. **Create and activate a virtual environment**

```bash
python3 -m venv .venv
source .venv/bin/activate   # On Windows: .venv\Scripts\activate
```

3. **Install dependencies**

```bash
pip install -r requirements.txt
```

4. **Create a .env file** with your Discord bot token or rename the `.env.example`:

You can set your prefix anything you want, best practice is usually `/` or `!`, but you can use it with words as well, like `!music`.

```bash
DISCORD_TOKEN=your_token_here
DISCORD_PREFIX=!your_prefix_here
```

5. **Run the bot**

```bash
python main.py
```

## Commands

All commands are prefixed with `/zene ` (with a space).

| Command                    | Description                           |
| -------------------------- | ------------------------------------- |
| `your_prefix join`         | Join the voice channel you're in      |
| `your_prefix leave`        | Leave the voice channel               |
| `your_prefix play <query>` | Play a YouTube video or search result |
| `your_prefix pause`        | Pause the music                       |
| `your_prefix resume`       | Resume paused music                   |
| `your_prefix skip`         | Skip the current track                |
| `your_prefix np`           | Show currently playing track          |

**✅ Example Usage**

```bash
your_prefix join
your_prefix play alan walker faded
your_prefix play https://www.youtube.com/watch?v=abc123
your_prefix skip
your_prefix pause
your_prefix resume
your_prefix np
```

## **🚨 Notes**

If a YouTube video requires login (age-restricted or private), yt-dlp may fail. See the yt-dlp cookies guide for more.

Make sure FFmpeg is installed and accessible globally via ffmpeg in the command line.

The bot creates a per-guild queue using asyncio.Queue() to manage independent sessions.

## **📄 License**

MIT License
