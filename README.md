# Twitch Live Announcer for Discord

Polls Twitch every 60 seconds and posts an embed in your chosen Discord channel
whenever a watched streamer goes live. Add as many Twitch usernames as you want
(yours, friends', anyone public) — no login from them is required.

## 1. Create the Discord bot

1. Go to https://discord.com/developers/applications → **New Application**.
2. Go to **Bot** → **Add Bot**. Copy the **Token** (this is `DISCORD_TOKEN`).
3. Under **Privileged Gateway Intents**, you don't need any of the special
   ones for this bot (no message content needed).
4. Go to **OAuth2 → URL Generator**:
   - Scopes: `bot`, `applications.commands`
   - Bot Permissions: `Send Messages`, `Embed Links`, `Read Message History`, `Mention Everyone`
   - Copy the generated URL, open it, and invite the bot to your server.

> The bot **pings @everyone** when someone goes live. Live announcements are
> followed by one random line picked from a list of 20 friendly "follow and
> support a fellow creator" messages. To ping @everyone the bot needs the
> **Mention Everyone** permission — grant it in the invite (as above) or in the
> announcement channel itself. `/setchannel` warns if that permission is
> missing.

## 2. Create a Twitch app (for read-only API access)

1. Go to https://dev.twitch.tv/console/apps → **Register Your Application**.
2. Name it anything, set OAuth Redirect URL to `http://localhost` (unused but required).
3. Category: "Application Integration" is fine.
4. Copy the **Client ID**. Click **New Secret** and copy the **Client Secret**.

This only uses the "app access token" flow to read public stream data —
you and your friends do **not** need to log in or authorize anything on Twitch.
Friends just need to give you their Twitch username.

## 3. Configure

```bash
cd twitch-discord-bot
cp .env.example .env
```

Edit `.env` and fill in:
```
DISCORD_TOKEN=...
TWITCH_CLIENT_ID=...
TWITCH_CLIENT_SECRET=...
```

## 4. Install and run

```bash
pip install -r requirements.txt
python bot.py
```

Keep this running (a small VPS, Raspberry Pi, or always-on PC works well —
free hosts like Railway or a home server are common choices).

### Windows: one-click start + auto-start

- **`start_bot.bat`** — installs dependencies and starts `bot.py`. It checks
  whether a bot is already running first (see *single-instance* below) and
  skips launching if so. The first time you run it, it asks whether you want
  the bot to **auto-start at Windows logon**; it only asks if the auto-start
  task isn't installed yet.
- **`install_service.ps1`** — registers a scheduled task named
  `TwitchDiscordBot` that starts the bot automatically at **logon** (not at
  boot). It runs `run_forever.ps1`, the restart wrapper, so the bot is
  relaunched after a short delay whenever it exits.
- **`run_forever.ps1`** — the wrapper the scheduled task runs. It starts
  `bot.py` with `python.exe` and **waits** for it to exit (using
  `Start-Process -Wait`), redirecting the bot's output to `logs/bot.out` and
  `logs/bot.err` and timestamping every start/exit into `logs/wrapper.log`.
  After each exit, whatever the bot printed is appended to
  `logs/crash-history.log` (rotated to `crash-history.log.old` past 512 KB) so
  a crash traceback is never lost to the next restart. Unless the exit was the
  "already running" case (code 3), the bot is relaunched with a **growing
  delay**: 10 seconds after the first quick failure, then 20, 40, 80, 160,
  capping at 5 minutes. The delay actually slept is recorded in
  `logs/wrapper.log`. Once the bot has run for at least 2 minutes, the next
  restart waits 10 seconds again and the sequence starts over.
- **`stop_auto_start.bat`** — disables auto-start: stops the running wrapper
  and bot process, then unregisters the `TwitchDiscordBot` task. Afterwards
  you launch the bot manually with `start_bot.bat`. It only kills a process on
  the bot's single-instance port if that process's command line actually
  contains `bot.py`; if another program holds that port, the script reports it
  and leaves it alone.
- **`stop_service.ps1`** — stops the scheduled task, kills the bot by the
  localhost port `bot.py` holds (only matching `Get-NetTCPConnection` entries
  in `Listen` state, and only when the owning process's command line contains
  `bot.py` — otherwise it reports that another program holds the port and
  leaves it alone), kills a `run_forever.ps1` wrapper only when its command
  line points at this folder, and unregisters the task. Prints exactly what it
  stopped, or that nothing was running. `stop_auto_start.bat` is a small
  launcher for this script.

Single-instance behavior: the bot binds a localhost TCP port
(`SINGLE_INSTANCE_PORT` in `bot.py`) for its whole lifetime. A second copy
fails to bind, writes a single line to stderr ("The bot appears to be already
running; exiting."), and silently exits with code 3 — it never touches the
log file the first copy is using. So two copies can never double-post
announcements or race writes to `data.json`. `start_bot.bat` reads the same
port from `bot.py` and skips launching when it is already listening.

## 5. Use it in Discord

In each server, in the channel where you want announcements:

```
/setchannel
```

Then add yourself and your friends (use Twitch **login** names, i.e. what's
in the URL `twitch.tv/<name>`, not the display name):

```
/addstreamer your_twitch_username
/addstreamer friend1_twitch_username
/addstreamer friend2_twitch_username
```

Other commands:
- `/liststreamers` — see who's watched in this server and who's currently live
- `/removestreamer <username>` — stop watching someone

Permissions: `/setchannel`, `/addstreamer` and `/removestreamer` require the
**Administrator** permission (the server owner always has it).
`/liststreamers` is open to **everyone** in the server. The required
permission is defined once by the `REQUIRED_PERMS` constant in `bot.py`, so
it's easy to change (e.g. to Manage Server if you prefer). Server admins can
further limit or open commands per role or per channel under **Server Settings
→ Integrations → the bot**, but the in-code permission check still applies to
the three admin commands no matter what is configured there.

That's it — the bot checks every 60 seconds and posts once each time someone
starts a stream (it won't spam while they stay live, and will post again next
time they go live). Each announcement is an embed with an **@everyone ping**
in the message text followed by a short support line. The @everyone ping is in
the invite (see section 1) so members are notified every time.

## 6. Custom games / tournament announcements

One-time setup per server (requires **Administrator**):
```
/setcustomschannel   (run in the channel you want these announcements posted in)
/settimezone         (pick from the list — this is how /create interprets times)
```

To post an announcement:
```
/create
```
This walks you through: pick the game (currently Marvel Rivals; Blur is listed
as "coming soon"), pick Tournament or Customs, then a popup asks for the
banner image URL, date & time (format `MM/DD HH:MM AM/PM`, e.g. `12/25 8:00 PM`,
interpreted using the server's `/settimezone` setting), room name, and room
password. The bot posts the announcement with `@everyone`, and everyone sees
the start time auto-converted to their own local time via Discord's built-in
timestamp formatting. A **Set Reminder** button on the post DMs anyone who
clicks it, then DMs them again 15 minutes before the event starts with the
room name/password. The button auto-disables once the event time passes.

To cancel/end an event early:
```
/end
```
Pick the event from the autocomplete dropdown (only this server's still-active
events are listed). The original announcement post is left as-is, but no
further reminders go out, and everyone who had clicked "Set Reminder" gets a
DM letting them know it was cancelled.

**Notes:**
- The banner image URL must be a direct link ending in `.png`, `.jpg`, `.jpeg`,
  `.gif`, or `.webp` — easiest way to get one: upload the image to any Discord
  channel, right-click it, and choose "Copy Link."
- Like the streamer list, this is entirely **per-server**: each server has its
  own customs channel, timezone, and event list, stored in the same
  `data.json`. Reminder data and button state survive bot restarts (the
  reminder loop and persistent-view registration both run at startup,
  mirroring how `check_streams` and the legacy-data migration already work).
- `/create`, `/end`, `/setcustomschannel` and `/settimezone` require
  **Administrator** (see `REQUIRED_PERMS` in `customs.py`).

## Notes / tweaks

- **Check interval**: change `CHECK_INTERVAL_SECONDS` in `bot.py` (default 60s).
  Twitch rate limits are generous enough for even a handful of streamers checked
  every 30s, but 60s is a safe default.
- **Offline grace**: a streamer is only marked offline after
  `OFFLINE_GRACE_CHECKS` (default 3) consecutive offline checks, so a brief
  Twitch blip doesn't cause a second "now live" post.
- **Logging**: the bot attaches a **rotating file handler** to the **root
  logger**, so the bot's own logger as well as `storage`, `twitch_api`, and the
  discord library all write to `logs/bot.log` (rotating at ~1 MB, 3 backups
  kept) — `logs/bot.log` is the complete record. A console handler is added
  **only when the bot runs in an interactive console** (stderr is a terminal),
  so running `python bot.py` in a terminal also echoes to the console. Under
  the scheduled task, stderr is a redirected file, so the console handler is
  not added; `logs/bot.err` therefore only ever contains real stderr output,
  such as Python tracebacks and startup errors (the wrapper appends it to
  `logs/crash-history.log` after each exit — see `run_forever.ps1` above). The
  wrapper also captures raw stdout to `logs/bot.out` and appends a timestamped
  history to `logs/wrapper.log`. A broken `data.json` is never deleted — it's
  moved to `data.json.corrupt-<timestamp>` and the bot keeps running.
- **Storage**: watched streamers and settings are saved in `data.json`, created
  automatically next to `bot.py`. Back it up if you move servers.
- **Multiple servers**: every server has its own announcement channel and its
  own streamer list. Run `/setchannel` and `/addstreamer` in each server you
  add the bot to. `/setchannel`, `/addstreamer` and `/removestreamer` require
  the **Administrator** permission (see `REQUIRED_PERMS` in `bot.py`);
  `/liststreamers` can be used by everyone in the server. Streamers watched in
  several servers are only looked up once per check.
- **Upgrading from the single-channel version**: on first start the bot moves
  your old `data.json` settings under the server that the old channel belongs
  to. Servers you haven't configured yet start empty. Old-format streamer
  entries that are missing a display name / profile picture are backfilled
  from Twitch once at startup.
