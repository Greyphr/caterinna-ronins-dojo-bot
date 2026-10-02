import logging
import os
import random
import re
import socket
import sys
import time
from logging.handlers import RotatingFileHandler
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

import customs
import storage
from twitch_api import TwitchAPI, TwitchRateLimited

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
TWITCH_CLIENT_ID = os.getenv("TWITCH_CLIENT_ID")
TWITCH_CLIENT_SECRET = os.getenv("TWITCH_CLIENT_SECRET")
CHECK_INTERVAL_SECONDS = 60
OFFLINE_GRACE_CHECKS = 3
SINGLE_INSTANCE_PORT = 47821
EXIT_ALREADY_RUNNING = 3

# Permission required by the admin commands (/setchannel, /addstreamer,
# /removestreamer). Changing this to {"manage_guild": True} would instead let
# anyone with Manage Server use those commands.
REQUIRED_PERMS = {"administrator": True}

SUPPORT_MESSAGES = [
    "💜 Drop by, say hi, and hit that follow, it means the world to a fellow creator!",
    "✨ Come hang out and show {name} some love. A follow goes a long way!",
    "🌸 Support a fellow creator today: stop in, chat a little, and follow if you like what you see!",
    "🫶 Every follow helps {name} grow. Come cheer them on!",
    "💫 Small streamers get big when we all show up. Go say hi and follow {name}!",
    "🎀 Be a sweetheart and go support {name}. Even one follow makes their day!",
    "🌟 Creators lift each other up! Swing by {name}'s stream and leave a follow!",
    "🍓 Be the reason {name} smiles today. Pop in, say hi, and follow!",
    "🦋 Behind every stream is a creator hoping someone shows up. Be that someone for {name}!",
    "💕 Free way to make someone's day: follow {name} and say hi in chat!",
    "🌈 We rise by lifting each other. Come support {name} and hit follow!",
    "🧸 Cozy vibes are waiting! Stop by {name}'s stream and share a little love.",
    "⭐ Every great streamer started with a few kind people who showed up. Be one of them for {name}!",
    "🍀 Good things happen when we support each other. Follow {name} and join the fun!",
    "🎶 Come for the stream, stay for the good company. A follow for {name} is appreciated!",
    "🌼 Kindness costs nothing. Go say hi to {name} and drop a follow!",
    "🔥 Help {name} keep the momentum going. Join the stream and follow!",
    "🫧 Little acts of support mean a lot to a growing creator. Follow {name} today!",
    "🎉 The community is better when we show up for each other. Go cheer on {name}!",
    "🌙 Whether it's your first visit or your fiftieth, {name} would love to see you there. Follow along!",
]

if not DISCORD_TOKEN or not TWITCH_CLIENT_ID or not TWITCH_CLIENT_SECRET:
    raise SystemExit(
        "Missing environment variables. Copy .env.example to .env and fill in "
        "DISCORD_TOKEN, TWITCH_CLIENT_ID, and TWITCH_CLIENT_SECRET."
    )

ANNOUNCE_CHANNEL_TYPES = (discord.TextChannel, discord.Thread, discord.VoiceChannel)
TWITCH_LOGIN_RE = re.compile(r"^[a-z0-9_]{1,25}$")

logger = logging.getLogger("twitch_bot")

# A socket kept open for the whole process lifetime so a second copy of the
# bot exits immediately instead of double-posting / racing writes.
_lock_socket = None
_ready_done = False

# Streamers announced since their last offline transition: (guild_id, login).
# Guards against re-posting when a data.json write fails after a send.
_announced: set = set()

# Twitch rate-limit backoff: skip check cycles until this time.
_rate_limited_until = 0.0

# Shuffle-bag that cycles through SUPPORT_MESSAGES (memory only, not saved).
_support_bag: list = []
_last_support_message: Optional[str] = None

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)
twitch = TwitchAPI(TWITCH_CLIENT_ID, TWITCH_CLIENT_SECRET)


def setup_logging() -> None:
    """Attach a RotatingFileHandler (logs/bot.log) to the ROOT logger, so every
    logger (twitch_bot, storage, twitch_api, discord.*) lands in the same place
    and nothing is lost under a hidden process. A console handler is added only
    when stderr is an interactive terminal: under run_forever.ps1 stderr is a
    redirected file, and echoing every log line there would bloat bot.err (and
    crash-history.log) without adding anything bot.log doesn't already have."""
    root = logging.getLogger()
    logs_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
    os.makedirs(logs_dir, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    # Drop handlers from an earlier setup call (tests / process restarts).
    for handler in list(root.handlers):
        if getattr(handler, "_bot_owned", False):
            root.removeHandler(handler)
            handler.close()

    file_handler = RotatingFileHandler(
        os.path.join(logs_dir, "bot.log"),
        maxBytes=1_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler._bot_owned = True
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    # Console echo only for a real interactive terminal. A broken isatty()
    # counts as "not a terminal", so a redirected stderr (a regular file) never
    # gets the console handler.
    err = sys.stderr
    if err is not None:
        try:
            is_tty = bool(getattr(err, "isatty", lambda: False)())
        except Exception:
            is_tty = False
        if is_tty:
            console_handler = logging.StreamHandler(err)
            console_handler._bot_owned = True
            console_handler.setFormatter(fmt)
            root.addHandler(console_handler)

    root.setLevel(logging.INFO)
    # The bot's own logger propagates to root; leave its level to the root.
    logger.setLevel(logging.NOTSET)


def acquire_single_instance_lock() -> None:
    """Bind a localhost TCP port. If it fails, another copy is already
    running: write one line to stderr and exit before we ever open the log
    file (which the first copy is already using)."""
    global _lock_socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", SINGLE_INSTANCE_PORT))
        sock.listen(1)
    except OSError:
        if sys.stderr is not None:
            print("The bot appears to be already running; exiting.", file=sys.stderr)
        sys.exit(EXIT_ALREADY_RUNNING)
    _lock_socket = sock  # keep a reference so the socket stays open


def _apply_rate_limit(reset_at: Optional[float]) -> None:
    """Set the rate-limit backoff window, honoring the server's reset time
    when it is sane, otherwise falling back to 60 seconds."""
    global _rate_limited_until
    now = time.time()
    if reset_at is not None and now < reset_at <= now + 300:
        _rate_limited_until = reset_at
    else:
        _rate_limited_until = now + 60


def normalize_twitch_username(raw: str) -> Optional[str]:
    """Normalize input into a Twitch login, or None if it can't be one.

    Accepts 'name', '@name', 'twitch.tv/name' and full URLs. Login names are
    lower-case letters, numbers and underscores, 1-25 chars (matches Twitch's
    rules). Whether the user actually exists is left to the Twitch API.
    """
    text = (raw or "").strip().lower()
    if not text:
        return None
    if "twitch.tv" in text:
        _, _, text = text.partition("twitch.tv")
        text = text.strip("/\\")
        text = text.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    text = text.lstrip("@\\")
    if not TWITCH_LOGIN_RE.match(text):
        return None
    return text


def get_support_message(display_name: str) -> str:
    """Return the next support line from a shuffled bag, so the same one never
    shows twice in a row across cycles. display_name is passed to .format by
    keyword only, so braces in a display name can't break anything."""
    global _support_bag, _last_support_message
    if not _support_bag:
        _support_bag = SUPPORT_MESSAGES.copy()
        random.shuffle(_support_bag)
        if _last_support_message is not None and len(_support_bag) > 1:
            for i in range(1, len(_support_bag)):
                if _support_bag[i] != _last_support_message:
                    _support_bag[0], _support_bag[i] = _support_bag[i], _support_bag[0]
                    break
    message = _support_bag.pop(0)
    _last_support_message = message
    return message.format(name=display_name)


async def migrate_legacy_data():
    """Older versions stored one global channel. Work out which server that
    channel belongs to and move the old settings under that server."""
    if not storage.has_legacy_data():
        return
    legacy_channel_id = storage.get_legacy_channel_id()
    if not legacy_channel_id:
        logger.warning("Old-format data found but no channel_id to place it under; leaving as is.")
        return
    try:
        channel = await bot.fetch_channel(legacy_channel_id)
    except discord.HTTPException:
        logger.warning("Old-format data found but its channel is unreachable; leaving as is.")
        return
    guild = getattr(channel, "guild", None)
    if guild is None:
        return
    storage.migrate_legacy(guild.id)
    logger.info("Migrated old settings into server '%s' (%s).", guild.name, guild.id)


async def backfill_legacy_entries():
    """Fetch display name / profile picture for old-format streamer entries
    that predate those fields. Failures are logged and skipped."""
    for guild_id, username in storage.get_incomplete_streamers():
        try:
            user_info = await twitch.get_user_info(username)
        except Exception:
            logger.exception("Could not backfill info for %s", username)
            continue
        if not user_info:
            continue
        storage.update_streamer_info(
            guild_id,
            username,
            display_name=user_info.get("display_name") or username,
            profile_image_url=user_info.get("profile_image_url") or "",
        )


async def setup_hook():
    await bot.tree.sync()
    if not check_streams.is_running():
        check_streams.start()
    if not bot.customs_reminder_loop.is_running():
        bot.customs_reminder_loop.start()


bot.setup_hook = setup_hook


@bot.event
async def on_ready():
    global _ready_done
    if _ready_done:
        return
    _ready_done = True
    logger.info("Logged in as %s", bot.user)
    await bot.change_presence(
        activity=discord.Activity(type=discord.ActivityType.watching, name="watching twitch 👀")
    )
    try:
        await migrate_legacy_data()
    except Exception:
        logger.exception("Failed to migrate legacy data")
    try:
        await backfill_legacy_entries()
    except Exception:
        logger.exception("Failed to backfill legacy entries")
    try:
        customs.register_persistent_views(bot)
    except Exception:
        logger.exception("Failed to register persistent customs views")


@bot.event
async def on_guild_remove(guild: discord.Guild):
    storage.remove_guild(guild.id)


_PERM_LABELS = {
    "administrator": "Administrator",
    "manage_guild": "Manage Server",
}


def _missing_permission_message(perms) -> str:
    """Message for has_permissions() failures. has_permissions requires ALL
    listed permissions, so one permission is an "A" message and several are an
    "A and B" message."""
    labels = [_PERM_LABELS.get(perm, perm.replace("_", " ").title()) for perm in perms]
    if len(labels) == 1:
        return f"You need the **{labels[0]}** permission to use this command."
    joined = "** and **".join(labels)
    return f"You need the **{joined}** permissions to use this command."


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        message = _missing_permission_message(REQUIRED_PERMS)
    else:
        logger.error("Command error", exc_info=error)
        message = "Something went wrong running that command."
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


@bot.tree.command(name="setchannel", description="Set this channel for live announcements")
@app_commands.guild_only()
@app_commands.default_permissions(**REQUIRED_PERMS)
@app_commands.checks.has_permissions(**REQUIRED_PERMS)
async def setchannel(interaction: discord.Interaction):
    if not isinstance(interaction.channel, ANNOUNCE_CHANNEL_TYPES):
        await interaction.response.send_message(
            "This command only works in a server text channel.",
            ephemeral=True,
        )
        return

    storage.set_channel_id(interaction.guild_id, interaction.channel_id)

    message = f"✅ Live announcements for this server will be posted in {interaction.channel.mention}"
    perms = interaction.channel.permissions_for(interaction.guild.me)
    if not (perms.send_messages and perms.embed_links):
        message += (
            "\n⚠️ I'm missing **Send Messages** and/or **Embed Links** in this channel. "
            "Fix that or announcements won't show up."
        )
    if not perms.mention_everyone:
        message += (
            "\n⚠️ I'm missing the **Mention Everyone** permission in this channel, "
            "so live announcements can't ping @everyone."
        )
    await interaction.response.send_message(message)


@bot.tree.command(name="addstreamer", description="Watch a Twitch username for live announcements")
@app_commands.describe(twitch_username="The Twitch login name (from twitch.tv/<n>), not the display name")
@app_commands.guild_only()
@app_commands.default_permissions(**REQUIRED_PERMS)
@app_commands.checks.has_permissions(**REQUIRED_PERMS)
async def addstreamer(interaction: discord.Interaction, twitch_username: str):
    username = normalize_twitch_username(twitch_username)
    if username is None:
        await interaction.response.send_message(
            "That doesn't look like a Twitch username. Use the login name from "
            "`twitch.tv/<name>` — letters, numbers and underscores, up to 25 characters.",
            ephemeral=True,
        )
        return

    await interaction.response.defer()
    try:
        user_info = await twitch.get_user_info(username)
    except TwitchRateLimited:
        await interaction.followup.send("Twitch is having trouble right now — please try again in a bit.")
        return
    except Exception:
        logger.exception("Unexpected Twitch error looking up %s", username)
        await interaction.followup.send("Twitch is having trouble right now — please try again.")
        return
    if not user_info:
        await interaction.followup.send(f"❌ Couldn't find a Twitch user named `{username}`.")
        return

    added = storage.add_streamer(
        interaction.guild_id,
        username,
        interaction.user.id,
        display_name=user_info.get("display_name", username),
        profile_image_url=user_info.get("profile_image_url", ""),
    )
    if not added:
        await interaction.followup.send(f"⚠️ `{username}` is already being watched in this server.")
        return

    message = f"✅ Now watching **{user_info['display_name']}** — I'll post when they go live."
    if not storage.get_channel_id(interaction.guild_id):
        message += "\n⚠️ No announcement channel is set for this server yet. Run `/setchannel` in the channel you want."
    await interaction.followup.send(message)


@bot.tree.command(name="removestreamer", description="Stop watching a Twitch username")
@app_commands.guild_only()
@app_commands.default_permissions(**REQUIRED_PERMS)
@app_commands.checks.has_permissions(**REQUIRED_PERMS)
async def removestreamer(interaction: discord.Interaction, twitch_username: str):
    username = normalize_twitch_username(twitch_username)
    if username is None:
        await interaction.response.send_message(
            "That doesn't look like a Twitch username.",
            ephemeral=True,
        )
        return
    removed = storage.remove_streamer(interaction.guild_id, username)
    if removed:
        await interaction.response.send_message(f"✅ Stopped watching `{username}`.")
    else:
        await interaction.response.send_message(f"⚠️ `{username}` wasn't being watched in this server.")


@bot.tree.command(name="liststreamers", description="List all watched Twitch streamers")
@app_commands.guild_only()
async def liststreamers(interaction: discord.Interaction):
    streamers = storage.get_streamers(interaction.guild_id)
    if not streamers:
        await interaction.response.send_message("No streamers are being watched in this server yet.")
        return
    lines = [
        f"• `{name}`" + (" 🔴 live" if info.get("is_live") else "")
        for name, info in streamers.items()
    ]
    await interaction.response.send_message("**Watched streamers:**\n" + "\n".join(lines))


async def resolve_channel(channel_id: int):
    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except discord.HTTPException:
            return None
    if not isinstance(channel, ANNOUNCE_CHANNEL_TYPES):
        return None
    return channel


def build_live_embed(username: str, info: dict, stream: dict) -> discord.Embed:
    display_name = stream.get("user_name") or info.get("display_name") or username
    profile_image_url = info.get("profile_image_url", "")

    embed = discord.Embed(
        title=stream.get("title") or "Live on Twitch!",
        url=f"https://twitch.tv/{username}",
        description=f"playing **{stream.get('game_name') or 'something'}**",
        color=discord.Color.purple(),
    )
    embed.set_author(
        name=f"{display_name} is now live!",
        url=f"https://twitch.tv/{username}",
        icon_url=profile_image_url or None,
    )
    if profile_image_url:
        embed.set_thumbnail(url=profile_image_url)

    thumb = stream.get("thumbnail_url", "")
    if thumb:
        thumb = thumb.replace("{width}", "440").replace("{height}", "248")
        # Cache-bust so Discord doesn't keep showing an old preview image.
        thumb = f"{thumb}?t={int(time.time())}"
        embed.set_image(url=thumb)
    embed.set_footer(text="Twitch")
    return embed


@tasks.loop(seconds=CHECK_INTERVAL_SECONDS)
async def check_streams():
    try:
        await _run_check_once()
    except Exception:
        logger.exception("check_streams cycle failed")


async def _run_check_once():
    if time.time() < _rate_limited_until:
        logger.info(
            "Skipping check: Twitch is rate-limited until %s.",
            time.strftime("%H:%M:%S", time.localtime(_rate_limited_until)),
        )
        return

    try:
        guilds = storage.get_all_guilds()
    except Exception:
        logger.exception("Could not load guild data")
        return

    # Drop in-memory announced keys for guilds/streamers that no longer exist,
    # so we never stop a re-post on stale state holding a name we no longer track.
    known = {
        (gid, name)
        for gid, g in guilds.items()
        for name in g.get("streamers", {})
    }
    global _announced
    _announced = {key for key in _announced if key in known}

    # Only servers that have both a channel and at least one streamer count.
    active = {
        gid: g
        for gid, g in guilds.items()
        if g.get("channel_id") and g.get("streamers")
    }
    if not active:
        return

    # One Twitch lookup for everyone across all servers.
    all_usernames = sorted({name for g in active.values() for name in g["streamers"]})
    try:
        live_now = await twitch.get_live_streams(all_usernames)
    except TwitchRateLimited as e:
        _apply_rate_limit(e.reset_at)
        logger.warning(
            "Twitch rate-limited us until %s; skipping this check cycle.",
            time.strftime("%H:%M:%S", time.localtime(_rate_limited_until)),
        )
        return
    except Exception:
        logger.exception("Error checking Twitch streams")
        return

    for guild_id, settings in active.items():
        channel = None  # resolved lazily, only if we actually need to post
        try:
            for username, info in settings["streamers"].items():
                try:
                    key = (guild_id, username)
                    announced = key in _announced
                    is_live_now = username in live_now
                    was_live = info.get("is_live", False) or announced

                    if is_live_now:
                        stream = live_now[username]
                        current_stream_id = stream.get("id")
                        stored_live = bool(info.get("is_live", False))

                        # Announce on a normal go-live, or when the streamer is
                        # live only according to the stored flag (the bot was
                        # off when the previous stream ended) and the stored
                        # stream id differs from the current one -- a NEW
                        # stream deserves its own announcement.
                        announce = not was_live
                        if not announce and stored_live and not announced:
                            stored_stream_id = info.get("stream_id")
                            if stored_stream_id is None:
                                # Old record without a stream id: treat it as
                                # the same stream, and quietly remember the
                                # current id so later comparisons work.
                                try:
                                    storage.set_live_status(
                                        int(guild_id), username, True, stream_id=current_stream_id
                                    )
                                except Exception:
                                    logger.exception(
                                        "[%s] Could not persist stream id for %s",
                                        guild_id,
                                        username,
                                    )
                            elif (
                                current_stream_id is not None
                                and stored_stream_id != current_stream_id
                            ):
                                announce = True

                        if announce:
                            if channel is None:
                                channel = await resolve_channel(settings["channel_id"])
                                if channel is None:
                                    logger.warning(
                                        "[%s] Announcement channel unreachable; skipping.",
                                        guild_id,
                                    )
                                    break

                            display_name = (
                                stream.get("user_name") or info.get("display_name") or username
                            )
                            try:
                                await channel.send(
                                    content=(
                                        f"🔴 **{display_name}** is now live! "
                                        f"https://twitch.tv/{username} @everyone\n"
                                        f"{get_support_message(display_name)}"
                                    ),
                                    embed=build_live_embed(username, info, stream),
                                    allowed_mentions=discord.AllowedMentions(
                                        everyone=True, users=False, roles=False
                                    ),
                                )
                            except discord.HTTPException:
                                # Don't mark as announced; we'll retry next cycle.
                                logger.exception(
                                    "[%s] Failed to post announcement for %s", guild_id, username
                                )
                                continue
                            # Mark announced immediately after the send; if the
                            # storage write below fails, this keeps us from
                            # posting a second @everyone next cycle.
                            _announced.add(key)
                            try:
                                storage.set_live_status(
                                    int(guild_id),
                                    username,
                                    True,
                                    stream_id=current_stream_id,
                                )
                            except Exception:
                                logger.exception(
                                    "[%s] Could not persist live status for %s",
                                    guild_id,
                                    username,
                                )
                        elif announced and not stored_live:
                            # We announced while running and only the stored flag
                            # is behind (a failed data.json write). Fix that
                            # quietly so a later crash can't cause a second
                            # @everyone.
                            try:
                                storage.set_live_status(
                                    int(guild_id),
                                    username,
                                    True,
                                    stream_id=current_stream_id,
                                )
                            except Exception:
                                logger.exception(
                                    "[%s] Could not persist live status for %s",
                                    guild_id,
                                    username,
                                )
                        # Seen live: any offline streak is over.
                        try:
                            storage.reset_missed_checks(int(guild_id), username)
                        except Exception:
                            logger.exception(
                                "[%s] Could not reset missed checks for %s", guild_id, username
                            )

                    elif was_live:
                        # Offline blips must repeat before we declare a stop,
                        # so a single missed check can't cause a re-post.
                        try:
                            missed = storage.bump_missed_checks(int(guild_id), username)
                        except Exception:
                            logger.exception(
                                "[%s] Could not bump missed checks for %s", guild_id, username
                            )
                            continue
                        if missed >= OFFLINE_GRACE_CHECKS:
                            try:
                                storage.set_live_status(int(guild_id), username, False)
                            except Exception:
                                logger.exception(
                                    "[%s] Could not persist offline status for %s",
                                    guild_id,
                                    username,
                                )
                            try:
                                storage.reset_missed_checks(int(guild_id), username)
                            except Exception:
                                logger.exception(
                                    "[%s] Could not reset missed checks for %s", guild_id, username
                                )
                            _announced.discard(key)
                except Exception:
                    logger.exception("[%s] Error handling streamer %s", guild_id, username)
        except Exception:
            logger.exception("[%s] Error processing guild", guild_id)


@check_streams.before_loop
async def before_check_streams():
    await bot.wait_until_ready()


@check_streams.error
async def on_check_streams_error(error: BaseException):
    logger.error("check_streams loop crashed: %s", error)
    check_streams.restart()


# Registers /create, /end, /settimezone, /setcustomschannel and its reminder
# loop. Called here (not at the top of the file) because it needs
# resolve_channel and ANNOUNCE_CHANNEL_TYPES, which are defined above.
customs.setup(bot, resolve_channel, ANNOUNCE_CHANNEL_TYPES)


def main() -> None:
    # Claim the single-instance lock first: a second copy exits with code 3
    # before we open the log file (the first copy already owns it).
    acquire_single_instance_lock()
    setup_logging()
    bot.run(
        DISCORD_TOKEN,
        log_handler=None,
        log_level=logging.INFO,
    )


if __name__ == "__main__":
    main()