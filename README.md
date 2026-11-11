# Bedtime Bot

At the times you set, the bot:
1. Posts an embed with a GIF in a text channel and pings people (by default, only whoever is in the voice channel).
2. Optionally joins the voice channel, plays a short sound, and leaves.

The GIF is either a random one from Giphy (optional) or your own file. If Giphy isn't set up or fails, it uses your file.

## 1. Set up on your laptop

Voice needs Python 3.10 or newer. Check with `python3 --version`.
If it's older, install a newer one first, e.g. `brew install python@3.12`, and use `python3.12` below.
Text and GIF posting work on older Pythons, but voice does not.

```bash
cd ~/Documents/bedtime-bot
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

For voice you also need `ffmpeg` and `opus`:

```bash
brew install ffmpeg opus
```

`requirements.txt` pins `davey==0.1.7` on purpose. See Troubleshooting for why.

## 2. Create the Discord bot

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications), click **New Application**, and name it.
2. **Bot** tab: click **Reset Token** and copy it (you only see it once). Turn on **Server Members Intent**.
3. **OAuth2 > URL Generator**: tick the `bot` scope, then these permissions: View Channels, Send Messages, Embed Links, Attach Files, Connect, Speak (plus Mention Everyone if you ping a role that isn't mentionable). Open the generated URL and add the bot to your server.
4. In Discord, turn on **Settings > Advanced > Developer Mode**, then right-click the channels (and role) and choose **Copy ID**.

## 3. Configure

```bash
cp .env.example .env
```

Open `.env` (it's a hidden file: press Cmd+Shift+. in Finder, or use `nano .env`) and fill in:
- `DISCORD_TOKEN`: the bot token. Keep it private. `.env` is in `.gitignore` so it won't be committed.
- `TEXT_CHANNEL_ID`, `VOICE_CHANNEL_ID`, `ROLE_ID` (optional). You can use the voice channel's ID for both text and voice to post in its built-in chat.
- `TIMEZONE`: e.g. `America/New_York`.
- `REMINDER_TIMES`: e.g. `23:45|30 minutes left,00:00|Go to bed!` (24-hour time, optional message after `|`). Order matters for `PLAY_AUDIO=last`.

If your `.env` was created before the Giphy options were added, add these lines to the end of it:

```
GIPHY_API_KEY=
GIPHY_QUERY=sleep
GIPHY_RATING=g
```

### GIF and sound

- **Your own files:** replace `assets/bedtime.gif` and `assets/bedtime.wav`, or point `GIF_PATH` / `AUDIO_PATH` at other files. ffmpeg reads most audio formats. Keep the GIF under about 10 MB.
- **Random Giphy GIF (optional):** get a free API key at [developers.giphy.com](https://developers.giphy.com) (create an app and choose **API**, not SDK) and set `GIPHY_API_KEY`. The bot picks a random result for `GIPHY_QUERY` each time and adds a "Powered by GIPHY" footer. The free beta key allows 100 calls per hour, far more than this needs.
- **Sound tips:** keep it about 5 to 15 seconds and loud, with a second of silence at the start so Discord doesn't clip the opening. This command does that for any file:
  ```bash
  ffmpeg -i input.mp3 -af "adelay=1000|1000,loudnorm=I=-16:TP=-1.5" -t 15 -ar 48000 -ac 2 assets/bedtime.wav
  ```
- Use only audio you have the rights to. Don't commit copyrighted tracks; add them to `.gitignore`.

## 4. Run it

```bash
cd ~/Documents/bedtime-bot
source venv/bin/activate
python bot.py
```

The terminal prints `Scheduler running`. Leave the window open and keep your laptop on and awake until your reminder times. Stop it with Ctrl+C.
To keep a Mac awake while it runs: `caffeinate -i python bot.py`.

You only need `source venv/bin/activate` once per Terminal window; the prompt shows `(venv)` when it's active.

### Test commands

| Command | What it does |
|---|---|
| `python bot.py --check` | Prints your schedule and which reminders play audio. Doesn't connect to Discord. |
| `python bot.py --test` | Sends one reminder right now (with audio, if configured) and exits. Join the voice channel first to hear it. |
| `python bot.py --voice-test` | Joins voice and alternates a loud tone ON and OFF four times, then exits. Use it to debug sound. |
| `python bot.py --debug` | Adds verbose voice and encryption logging to any of the above. |

## Options in .env

| Setting | Values | What it does |
|---|---|---|
| `MENTION_MODE` | `voice`, `role`, `everyone`, `none` | Who gets pinged. `voice` pings only people in the voice channel; `role` pings `ROLE_ID` |
| `ONLY_IF_IN_VOICE` | `true` / `false` | Skip the reminder when nobody is in voice |
| `PLAY_AUDIO` | `all`, `last`, `none` | Which reminders also play the sound. `last` = the last one listed in `REMINDER_TIMES` |
| `GIPHY_API_KEY` / `GIPHY_QUERY` / `GIPHY_RATING` | key / search words / `g`, `pg`, `pg-13`, `r` | Random Giphy GIF. Blank key = use the fallback |
| `GIF_PATH` / `GIF_URL` | file path or link | Fallback GIF when Giphy isn't used or fails |
| `AUDIO_PATH` | file path | The sound to play in voice |
| `MAX_AUDIO_SECONDS` | number of seconds (default `300`) | Safety cap: the bot stops playing and leaves voice after this long, even if the clip is longer |

The bot only joins voice when someone is in the voice channel.

## Moving it to a Raspberry Pi or VPS later

Copy the folder (including `.env`), then run the same install steps there. Create a new `venv` on the new machine rather than copying the old one. To start it on boot and restart it if it crashes, use a systemd service. The bot needs to be running at the reminder times, since it does its own scheduling.

## Troubleshooting

- **Nothing happens at the scheduled time:** check `TIMEZONE` and that the program is still running and the laptop is awake.
- **"Missing Access" / "Forbidden":** the bot needs View Channel, Send Messages, Embed Links, Attach Files (text) and Connect, Speak (voice) on those channels.
- **The bot joins voice but you hear nothing:** make sure you're using `davey==0.1.7` (`pip list | grep davey`). With davey 0.2.0 and discord.py 2.7.1, Discord's voice encryption never becomes ready, so the bot sends audio that Discord drops. The speaking ring still lights up, which makes it confusing. Run `python bot.py --voice-test --debug` and look for `ready=True can_encrypt=True`. If a newer discord.py fixes this, the pin can be removed.
- **Also check:** the bot has Speak permission, isn't muted, its volume (right-click it in the call) isn't low, and Discord is up to date.
- **Voice fails with an opus error:** `brew install opus`.
- **Can't see who's in voice:** turn on Server Members Intent in the Developer Portal (Bot tab).
- **Giphy warning in the log (HTTP 403, no results):** check `GIPHY_API_KEY`. The bot falls back to your own GIF either way.
- **Keep the token secret.** If it leaks, reset it in the Developer Portal.
