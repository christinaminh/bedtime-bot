"""Bedtime Bot: posts a GIF + pings, and plays a sound in voice, at set times.

Run:        python bot.py
Test now:   python bot.py --test        (fires one reminder immediately, then exits)
Dry run:    python bot.py --check       (prints the schedule, no Discord connection)
"""
import argparse
import asyncio
import ctypes.util
import logging
import os
import sys
from datetime import datetime, time as dtime
from pathlib import Path
from typing import List, Optional, Tuple

try:
    from zoneinfo import ZoneInfo  # Python 3.9+
except ImportError:  # Python 3.8
    from backports.zoneinfo import ZoneInfo  # type: ignore

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

log = logging.getLogger("bedtime-bot")

DEFAULT_TEXT = "It's getting late. Time to wrap up and go to bed! 😴"


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
def env_int(name: str, required: bool = False) -> Optional[int]:
    raw = os.getenv(name, "").strip()
    if not raw:
        if required:
            sys.exit("Missing required setting %s in .env" % name)
        return None
    try:
        return int(raw)
    except ValueError:
        sys.exit("%s must be a number (a Discord ID), got %r" % (name, raw))


def parse_reminders(raw: str) -> List[Tuple[dtime, str]]:
    """'23:45|30 min left,00:00' -> [(23:45, '30 min left'), (00:00, DEFAULT_TEXT)]"""
    out = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        when, _, text = part.partition("|")
        try:
            hh, mm = when.strip().split(":")
            t = dtime(int(hh), int(mm))
        except ValueError:
            sys.exit("Bad time %r in REMINDER_TIMES. Use 24-hour HH:MM, e.g. 23:45" % when)
        out.append((t, text.strip() or DEFAULT_TEXT))
    if not out:
        sys.exit("REMINDER_TIMES is empty. Example: 23:45|30 minutes left,00:00")
    # Keep the order you wrote them: "last" means the last one listed, which matters
    # when a bedtime sequence crosses midnight (23:45 then 00:00).
    return out


def resolve_path(name: str) -> Optional[Path]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    p = Path(raw)
    return p if p.is_absolute() else BASE_DIR / p


class Config:
    def __init__(self) -> None:
        self.token = os.getenv("DISCORD_TOKEN", "").strip()
        self.text_channel_id = env_int("TEXT_CHANNEL_ID", required=True)
        self.voice_channel_id = env_int("VOICE_CHANNEL_ID")
        self.role_id = env_int("ROLE_ID")
        self.tz = ZoneInfo(os.getenv("TIMEZONE", "America/Los_Angeles").strip())
        self.reminders = parse_reminders(os.getenv("REMINDER_TIMES", "23:45|30 minutes until bedtime,00:00"))
        # voice | role | everyone | none
        self.mention_mode = os.getenv("MENTION_MODE", "voice").strip().lower()
        # If true and nobody is in the voice channel, skip the reminder entirely.
        self.only_if_in_voice = os.getenv("ONLY_IF_IN_VOICE", "false").strip().lower() == "true"
        # all | last | none: which reminders also play the audio clip.
        self.play_audio = os.getenv("PLAY_AUDIO", "last").strip().lower()
        self.gif_path = resolve_path("GIF_PATH")
        self.gif_url = os.getenv("GIF_URL", "").strip()
        # Optional: pull a random GIF from Giphy each time, falling back to GIF_PATH / GIF_URL.
        self.giphy_key = os.getenv("GIPHY_API_KEY", "").strip()
        self.giphy_query = os.getenv("GIPHY_QUERY", "sleep").strip() or "sleep"
        self.giphy_rating = os.getenv("GIPHY_RATING", "g").strip().lower() or "g"
        if self.giphy_rating not in ("g", "pg", "pg-13", "r"):
            sys.exit("GIPHY_RATING must be g, pg, pg-13, or r")
        self.audio_path = resolve_path("AUDIO_PATH")
        # Safety cap: stop playing and leave voice after this many seconds, even if the clip is longer.
        self.max_audio_seconds = env_int("MAX_AUDIO_SECONDS") or 300
        if self.max_audio_seconds < 1:
            sys.exit("MAX_AUDIO_SECONDS must be at least 1")
        self.title = os.getenv("EMBED_TITLE", "🌙 Bedtime check").strip()

        if self.mention_mode not in ("voice", "role", "everyone", "none"):
            sys.exit("MENTION_MODE must be voice, role, everyone, or none")
        if self.mention_mode == "role" and not self.role_id:
            sys.exit("MENTION_MODE=role needs ROLE_ID")
        if self.play_audio not in ("all", "last", "none"):
            sys.exit("PLAY_AUDIO must be all, last, or none")
        if self.play_audio != "none" and not self.voice_channel_id:
            log.warning("No VOICE_CHANNEL_ID set, so audio is disabled.")
            self.play_audio = "none"

    def describe(self) -> str:
        lines = ["Time zone: %s" % self.tz, "Reminders:"]
        for i, (t, text) in enumerate(self.reminders):
            audio = self.play_audio == "all" or (self.play_audio == "last" and i == len(self.reminders) - 1)
            lines.append("  %s  %s%s" % (t.strftime("%H:%M"), text, "  [+audio]" if audio else ""))
        lines.append("Mentions: %s" % self.mention_mode)
        if self.giphy_key:
            lines.append("GIF: random Giphy search %r (rating %s), fallback: %s"
                         % (self.giphy_query, self.giphy_rating, self.gif_path or self.gif_url or "none"))
        else:
            lines.append("GIF: %s" % (self.gif_path or self.gif_url or "none"))
        lines.append("Audio file: %s" % (self.audio_path or "none"))
        return "\n".join(lines)


# --------------------------------------------------------------------------
# Scheduling (pure logic, no Discord)
# --------------------------------------------------------------------------
def due_reminders(now: datetime, reminders: List[Tuple[dtime, str]], fired: set) -> List[int]:
    """Indexes of reminders whose minute is `now` and that haven't fired today."""
    due = []
    for i, (t, _) in enumerate(reminders):
        key = (now.date(), i)
        if now.hour == t.hour and now.minute == t.minute and key not in fired:
            due.append(i)
    return due


# --------------------------------------------------------------------------
# Discord side
# --------------------------------------------------------------------------
def load_opus() -> None:
    import discord

    if discord.opus.is_loaded():
        return
    for name in ("opus", "libopus.so.0", "/opt/homebrew/lib/libopus.dylib", "/usr/local/lib/libopus.dylib"):
        path = ctypes.util.find_library(name) or name
        try:
            discord.opus.load_opus(path)
            return
        except Exception:
            continue
    log.warning("Could not load libopus. Voice playback will fail (macOS: brew install opus).")


def build_bot(cfg: Config):
    import discord

    intents = discord.Intents.default()
    intents.members = True  # needed to see who is in the voice channel
    intents.voice_states = True
    client = discord.Client(intents=intents)
    client.cfg = cfg  # type: ignore[attr-defined]
    client.fired = set()  # type: ignore[attr-defined]
    return client


GIPHY_SEARCH_URL = "https://api.giphy.com/v1/gifs/search"
_last_giphy_id = None  # avoid showing the same GIF twice in a row


async def fetch_giphy(cfg: Config) -> Optional[str]:
    """Return a random GIF URL from a Giphy search, or None on any failure.

    Never logs the request URL, because it contains the API key.
    """
    global _last_giphy_id
    if not cfg.giphy_key:
        return None
    import random

    import aiohttp

    params = {
        "api_key": cfg.giphy_key,
        "q": cfg.giphy_query,
        "limit": 50,
        "offset": random.randint(0, 50),
        "rating": cfg.giphy_rating,
        "lang": "en",
    }
    try:
        timeout = aiohttp.ClientTimeout(total=5)  # don't hold up the reminder
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(GIPHY_SEARCH_URL, params=params) as resp:
                if resp.status != 200:
                    log.warning("Giphy returned HTTP %s (check GIPHY_API_KEY). Using fallback GIF.", resp.status)
                    return None
                payload = await resp.json()
    except Exception as exc:
        log.warning("Giphy request failed (%s). Using fallback GIF.", type(exc).__name__)
        return None

    choices = []
    for item in payload.get("data", []):
        images = item.get("images", {})
        # Prefer a moderately sized version so it loads quickly and stays under upload limits.
        url = (images.get("downsized") or images.get("fixed_height") or images.get("original") or {}).get("url")
        if url and item.get("id") != _last_giphy_id:
            choices.append((item.get("id"), url))
    if not choices:
        log.warning("Giphy returned no results for %r. Using fallback GIF.", cfg.giphy_query)
        return None
    gif_id, url = random.choice(choices)
    _last_giphy_id = gif_id
    return url


async def send_reminder(client, cfg: Config, text: str, with_audio: bool) -> None:
    import discord

    channel = client.get_channel(cfg.text_channel_id) or await client.fetch_channel(cfg.text_channel_id)
    voice_channel = None
    if cfg.voice_channel_id:
        voice_channel = client.get_channel(cfg.voice_channel_id) or await client.fetch_channel(cfg.voice_channel_id)

    members = [m for m in (voice_channel.members if voice_channel else []) if not m.bot]

    if cfg.only_if_in_voice and not members:
        log.info("Nobody in voice, skipping (ONLY_IF_IN_VOICE=true).")
        return

    # Who to ping
    mention = ""
    allowed = discord.AllowedMentions.none()
    if cfg.mention_mode == "voice" and members:
        mention = " ".join(m.mention for m in members)
        allowed = discord.AllowedMentions(users=True)
    elif cfg.mention_mode == "role" and cfg.role_id:
        mention = "<@&%d>" % cfg.role_id
        allowed = discord.AllowedMentions(roles=True)
    elif cfg.mention_mode == "everyone":
        mention = "@everyone"
        allowed = discord.AllowedMentions(everyone=True)

    embed = discord.Embed(title=cfg.title, description=text, color=0x5B6EE1)
    file = None
    giphy_gif = await fetch_giphy(cfg)
    if giphy_gif:
        embed.set_image(url=giphy_gif)
        embed.set_footer(text="Powered by GIPHY")  # Giphy's API terms ask for attribution
    elif cfg.gif_path and cfg.gif_path.exists():
        file = discord.File(str(cfg.gif_path), filename=cfg.gif_path.name)
        embed.set_image(url="attachment://%s" % cfg.gif_path.name)
    elif cfg.gif_url:
        embed.set_image(url=cfg.gif_url)

    kwargs = dict(content=mention or None, embed=embed, allowed_mentions=allowed)
    if file:
        kwargs["file"] = file
    await channel.send(**kwargs)
    log.info("Posted reminder: %s", text)

    if with_audio and voice_channel and members:
        await play_clip(client, cfg, voice_channel)
    elif with_audio and voice_channel and not members:
        log.info("Nobody in voice, not joining to play audio.")


async def play_clip(client, cfg: Config, voice_channel) -> None:
    import discord

    if not cfg.audio_path or not cfg.audio_path.exists():
        log.warning("AUDIO_PATH missing or file not found, skipping audio.")
        return
    load_opus()

    me = voice_channel.guild.me
    perms = voice_channel.permissions_for(me)
    if not perms.connect:
        log.error("The bot lacks the Connect permission in #%s.", voice_channel.name)
        return
    if not perms.speak:
        log.error(
            "The bot lacks the SPEAK permission in #%s. It can join but will be silent. "
            "Give the bot's role 'Speak' on this channel (or in Server Settings > Roles).",
            voice_channel.name,
        )

    vc = None
    try:
        vc = await voice_channel.connect(timeout=20, reconnect=False)
        vs = me.voice
        if vs:
            log.info(
                "Bot voice state: server_mute=%s suppress=%s self_mute=%s",
                vs.mute, vs.suppress, vs.self_mute,
            )
            if vs.mute or vs.suppress:
                log.error("The bot is muted/suppressed in this channel, so nobody will hear it.")

        # Discord requires end-to-end encrypted voice (DAVE). Audio sent before the encryption
        # session is ready is dropped by clients, so wait for it instead of playing silently.
        conn = getattr(vc, "_connection", None)
        waited = 0.0
        while conn is not None and not getattr(conn, "can_encrypt", True) and waited < 10:
            await asyncio.sleep(0.25)
            waited += 0.25
        if conn is not None and not getattr(conn, "can_encrypt", True):
            log.error(
                "Voice encryption never became ready, so the audio would be silent. %s "
                "(Known cause: davey 0.2.0 with discord.py 2.7.1. Use davey==0.1.7.)",
                dave_status(vc),
            )
        done = asyncio.Event()
        loop = asyncio.get_running_loop()

        def after(err):
            if err:
                log.error("Playback error: %s", err)
            loop.call_soon_threadsafe(done.set)

        vc.play(discord.FFmpegPCMAudio(str(cfg.audio_path)), after=after)
        log.info("Started playing %s (connected=%s, playing=%s)", cfg.audio_path.name, vc.is_connected(), vc.is_playing())
        await asyncio.wait_for(done.wait(), timeout=cfg.max_audio_seconds)
        log.info("Finished audio clip.")
    except Exception as exc:
        log.error("Voice playback failed: %s", exc)
    finally:
        if vc and vc.is_connected():
            await vc.disconnect(force=True)


def dave_status(vc) -> str:
    """Describe Discord's end-to-end voice encryption (DAVE) state, for debugging silence."""
    conn = getattr(vc, "_connection", None)
    if conn is None:
        return "no connection object"
    session = getattr(conn, "dave_session", None)

    def attr(name):
        try:
            return getattr(session, name)
        except Exception as exc:  # diagnostics must never crash the test
            return "err:%s" % exc

    extra = ""
    if session is not None:
        try:
            users = session.get_user_ids()
        except Exception as exc:
            users = "err:%s" % exc
        extra = " status=%s epoch=%s group_users=%s leaf=%s passthrough=%s" % (
            attr("status"), attr("epoch"), users, attr("own_leaf_index"), attr("can_passthrough"),
        )
    return "dave_protocol_version=%s session=%s ready=%s can_encrypt=%s%s" % (
        getattr(conn, "dave_protocol_version", "?"),
        "yes" if session is not None else "NO",
        attr("ready") if session is not None else None,
        getattr(conn, "can_encrypt", "?"),
        extra,
    )


async def run_voice_test(client, cfg: Config, rounds: int = 4, gap: int = 3) -> None:
    """Join voice and alternate a loud tone (ON) with silence (OFF) so you can watch the ring."""
    import discord

    if not cfg.voice_channel_id:
        log.error("Set VOICE_CHANNEL_ID in .env first.")
        return
    path = BASE_DIR / "assets" / "diagnostic.wav"
    if not path.exists():
        log.error("Missing %s", path)
        return
    load_opus()
    channel = client.get_channel(cfg.voice_channel_id) or await client.fetch_channel(cfg.voice_channel_id)
    vc = await channel.connect(timeout=20, reconnect=False)
    try:
        loop = asyncio.get_running_loop()
        log.info("Members in voice channel: %s", [(m.display_name, m.id) for m in channel.members])
        log.info("Right after joining: %s", dave_status(vc))
        await asyncio.sleep(3)  # give the encryption handshake time to finish
        log.info("3s after joining:    %s", dave_status(vc))
        for n in range(1, rounds + 1):
            done = asyncio.Event()
            log.info("Round %d/%d: ON  (tone for ~4.5s, the ring should appear) | %s", n, rounds, dave_status(vc))
            vc.play(
                discord.FFmpegPCMAudio(str(path)),
                after=lambda err: loop.call_soon_threadsafe(done.set),
            )
            await done.wait()
            log.info("Round %d/%d: OFF (silence for %ds, the ring should disappear)", n, rounds, gap)
            await asyncio.sleep(gap)
    finally:
        if vc.is_connected():
            await vc.disconnect(force=True)
    log.info("Voice test finished.")


def run(cfg: Config, test: bool, voice_test: bool = False) -> None:
    import discord

    if not cfg.token:
        sys.exit("DISCORD_TOKEN is missing. Put it in .env (see .env.example).")

    client = build_bot(cfg)

    async def scheduler():
        await client.wait_until_ready()
        log.info("Scheduler running. %s", "TEST MODE" if test else "")
        if voice_test:
            await run_voice_test(client, cfg)
            await client.close()
            return
        if test:
            await send_reminder(client, cfg, "🧪 Test reminder. " + DEFAULT_TEXT, with_audio=cfg.play_audio != "none")
            await client.close()
            return
        while not client.is_closed():
            now = datetime.now(cfg.tz)
            for i in due_reminders(now, cfg.reminders, client.fired):
                client.fired.add((now.date(), i))
                is_last = i == len(cfg.reminders) - 1
                with_audio = cfg.play_audio == "all" or (cfg.play_audio == "last" and is_last)
                try:
                    await send_reminder(client, cfg, cfg.reminders[i][1], with_audio)
                except Exception:
                    log.exception("Reminder failed")
            # forget old days
            client.fired = {k for k in client.fired if k[0] == now.date()}
            await asyncio.sleep(15)

    @client.event
    async def on_ready():
        log.info("Logged in as %s", client.user)
        if not getattr(client, "_sched_started", False):
            client._sched_started = True
            client.loop.create_task(scheduler())

    client.run(cfg.token, log_handler=None)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", action="store_true", help="send one reminder now and exit")
    parser.add_argument("--check", action="store_true", help="print the schedule and exit")
    parser.add_argument("--debug", action="store_true", help="verbose voice logging")
    parser.add_argument("--voice-test", action="store_true", help="join voice and alternate a tone ON/OFF, then exit")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.debug:
        # Only the voice loggers, to keep the noisy gateway logs out of the output.
        for name in ("discord.voice_state", "discord.voice_client", "discord.player"):
            logging.getLogger(name).setLevel(logging.DEBUG)

        # Voice-encryption (DAVE/MLS) handshake steps from the gateway logger. Filtered to just
        # those lines so keys and session details in other debug output are never printed.
        class _DaveOnly(logging.Filter):
            def filter(self, record: logging.LogRecord) -> bool:
                msg = record.getMessage()
                return record.levelno >= logging.WARNING or any(
                    k in msg for k in ("DAVE", "MLS", "binary frame", "transition", "epoch")
                )

        gw = logging.getLogger("discord.gateway")
        gw.setLevel(logging.DEBUG)
        gw.addFilter(_DaveOnly())
    cfg = Config()
    print(cfg.describe())
    if args.check:
        return
    run(cfg, test=args.test, voice_test=args.voice_test)


if __name__ == "__main__":
    main()
