import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import tasks

import storage
from timezones import TIMEZONE_CHOICES

logger = logging.getLogger("customs")

REMINDER_MINUTES_BEFORE = 15
CHECK_INTERVAL_SECONDS = 30

# Permission required for /create, /end, /setcustomschannel, /settimezone.
# Kept separate from bot.py's REQUIRED_PERMS to avoid a circular import;
# change both together if you want a different permission.
REQUIRED_PERMS = {"administrator": True}

BANNER_URL_PATTERN = re.compile(
    r"^https?://\S+\.(png|jpg|jpeg|gif|webp)(\?\S*)?$", re.IGNORECASE
)

GAME_CHOICES = ["Marvel Rivals", "Blur"]
TYPE_CHOICES = ["Tournament", "Customs"]

# Set once by setup(); kept at module level since the Modal/View classes
# below are defined at import time, before setup() runs. resolve_channel
# mirrors bot.py's own helper (resolves an ID to a real, sendable channel or
# None) so we don't duplicate that logic or import bot.py (which imports us).
_resolve_channel = None
_announce_channel_types = None


def _parse_event_datetime(text: str, tz_name: str):
    """Parses 'MM/DD HH:MM AM/PM' in the given IANA timezone.
    Returns a timezone-aware datetime, or None if unparseable or in the past."""
    cleaned = text.strip().upper().replace(".", "")
    formats = ["%m/%d %I:%M %p", "%m/%d %I:%M%p", "%m/%d %H:%M"]

    parsed = None
    for fmt in formats:
        try:
            parsed = datetime.strptime(cleaned, fmt)
            break
        except ValueError:
            continue
    if parsed is None:
        return None

    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        logger.exception("Invalid stored timezone %r", tz_name)
        return None

    now_local = datetime.now(tz)
    candidate = parsed.replace(year=now_local.year, tzinfo=tz)

    # If the date/time already passed this year, assume they mean next year.
    if candidate < now_local - timedelta(minutes=5):
        candidate = candidate.replace(year=now_local.year + 1)

    if candidate <= now_local:
        return None

    return candidate


def _build_event_embed(event: dict) -> discord.Embed:
    ts = event["timestamp"]
    event_type = event["type"]
    color = discord.Color.red() if event_type == "Tournament" else discord.Color.blue()

    flavor = (
        f"👀 Who's feeling customizable cuz I am! Rivals **{event_type}** "
        f"at <t:{ts}:t> (<t:{ts}:R>) in the Dojo!"
    )

    embed = discord.Embed(
        title=f"Marvel Rivals {event_type}",
        description=flavor,
        color=color,
    )
    embed.set_image(url=event["banner_url"])
    embed.add_field(name="🕒 Starts", value=f"<t:{ts}:F>", inline=False)
    embed.add_field(name="Room Name", value=event["room_name"], inline=True)
    embed.add_field(name="Room Password", value=event["room_password"], inline=True)
    embed.set_footer(text="Click Set Reminder to get a DM 15 minutes before it starts!")
    return embed


class ReminderView(discord.ui.View):
    """Persistent view (timeout=None) so the button keeps working across bot
    restarts. guild_id/event_id are passed in directly rather than parsed
    back out of custom_id, since we always know both when constructing this
    (either right after posting, or while re-registering on_ready)."""

    def __init__(self, guild_id: int, event_id: str, disabled: bool = False):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.event_id = event_id
        button = discord.ui.Button(
            label="⏰ Set Reminder",
            style=discord.ButtonStyle.primary,
            custom_id=f"remind_{guild_id}_{event_id}",
            disabled=disabled,
        )
        button.callback = self._on_click
        self.add_item(button)

    async def _on_click(self, interaction: discord.Interaction):
        event = storage.get_event(self.guild_id, self.event_id)
        if event is None:
            await interaction.response.send_message("This event no longer exists.", ephemeral=True)
            return
        if event.get("ended"):
            await interaction.response.send_message("This event has been cancelled/ended.", ephemeral=True)
            return

        now = datetime.now(timezone.utc)
        event_dt = datetime.fromtimestamp(event["timestamp"], tz=timezone.utc)
        if now >= event_dt:
            await interaction.response.send_message("This event has already started.", ephemeral=True)
            return

        added = storage.add_reminder_user(self.guild_id, self.event_id, interaction.user.id)
        if not added:
            await interaction.response.send_message(
                "You're already set for a reminder on this one!", ephemeral=True
            )
            return

        try:
            await interaction.user.send(
                f"Got you! 🎮 I'll let you know when it's {REMINDER_MINUTES_BEFORE} minutes "
                f"to the Marvel Rivals {event['type'].lower()}."
            )
        except discord.Forbidden:
            pass  # DMs closed; the reminder just won't arrive, not worth erroring over.
        except discord.HTTPException:
            logger.exception("Could not DM %s after Set Reminder click", interaction.user.id)

        await interaction.response.send_message(
            f"✅ I'll remind you {REMINDER_MINUTES_BEFORE} minutes before it starts!", ephemeral=True
        )


class MarvelRivalsModal(discord.ui.Modal):
    def __init__(self, event_type: str):
        super().__init__(title=f"Marvel Rivals {event_type}")
        self.event_type = event_type

        self.banner_url = discord.ui.TextInput(
            label="Banner Image URL",
            placeholder="https://i.imgur.com/example.png",
            required=True,
        )
        self.datetime_str = discord.ui.TextInput(
            label="Date & Time (MM/DD HH:MM AM/PM)",
            placeholder="12/25 8:00 PM",
            required=True,
        )
        self.room_name = discord.ui.TextInput(label="Room Name", required=True)
        self.room_password = discord.ui.TextInput(label="Room Password", required=True)

        self.add_item(self.banner_url)
        self.add_item(self.datetime_str)
        self.add_item(self.room_name)
        self.add_item(self.room_password)

    async def on_submit(self, interaction: discord.Interaction):
        guild_id = interaction.guild_id

        customs_channel_id = storage.get_customs_channel_id(guild_id)
        if not customs_channel_id:
            await interaction.response.send_message(
                "❌ No customs/tournament channel is set yet. Run `/setcustomschannel` in the "
                "channel you want announcements posted in first.",
                ephemeral=True,
            )
            return

        tz_name = storage.get_timezone(guild_id)
        if not tz_name:
            await interaction.response.send_message(
                "❌ No server timezone is set yet. Run `/settimezone` first so I know how to "
                "interpret the time you enter.",
                ephemeral=True,
            )
            return

        banner_url = self.banner_url.value.strip()
        if not BANNER_URL_PATTERN.match(banner_url):
            await interaction.response.send_message(
                "❌ That doesn't look like a valid direct image link. It needs to start with "
                "http(s):// and end in .png, .jpg, .jpeg, .gif, or .webp. Please run `/create` "
                "again with a fixed link.",
                ephemeral=True,
            )
            return

        event_dt = _parse_event_datetime(self.datetime_str.value, tz_name)
        if event_dt is None:
            await interaction.response.send_message(
                "❌ Couldn't understand that date/time, or it's already in the past. Please use "
                "the format `MM/DD HH:MM AM/PM`, e.g. `12/25 8:00 PM`, and run `/create` again.",
                ephemeral=True,
            )
            return

        channel = await _resolve_channel(customs_channel_id)
        if channel is None:
            await interaction.response.send_message(
                "❌ I can't access the configured customs channel anymore (it may have been "
                "deleted, or I lost permissions). Run `/setcustomschannel` again.",
                ephemeral=True,
            )
            return

        event_id = uuid.uuid4().hex[:8]
        event = {
            "game": "Marvel Rivals",
            "type": self.event_type,
            "banner_url": banner_url,
            "timestamp": int(event_dt.timestamp()),
            "room_name": self.room_name.value.strip(),
            "room_password": self.room_password.value.strip(),
            "created_by": interaction.user.id,
            "channel_id": customs_channel_id,
            "message_id": None,
            "ended": False,
            "reminder_sent": False,
            "started_announced": False,
            "disabled": False,
            "reminding_users": [],
        }
        storage.create_event(guild_id, event_id, event)

        embed = _build_event_embed(event)
        view = ReminderView(guild_id, event_id)
        try:
            message = await channel.send(
                content="@everyone",
                embed=embed,
                view=view,
                allowed_mentions=discord.AllowedMentions(everyone=True, users=False, roles=False),
            )
        except discord.HTTPException:
            logger.exception("[%s] Failed to post customs announcement", guild_id)
            await interaction.response.send_message(
                "❌ Something went wrong posting that announcement. Nothing was sent.",
                ephemeral=True,
            )
            return

        storage.update_event(guild_id, event_id, message_id=message.id)
        await interaction.response.send_message(
            f"✅ Announcement posted in {channel.mention}!", ephemeral=True
        )


class TypeSelect(discord.ui.Select):
    def __init__(self):
        options = [discord.SelectOption(label=t) for t in TYPE_CHOICES]
        super().__init__(placeholder="Tournament or Customs?", options=options)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(MarvelRivalsModal(self.values[0]))


class TypeSelectView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)
        self.add_item(TypeSelect())


class GameSelect(discord.ui.Select):
    def __init__(self):
        options = [discord.SelectOption(label=g) for g in GAME_CHOICES]
        super().__init__(placeholder="Which game?", options=options)

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "Blur":
            await interaction.response.edit_message(
                content="🚧 Blur customs/tournaments are coming soon!", view=None
            )
            return
        await interaction.response.edit_message(
            content="Tournament or Customs?", view=TypeSelectView()
        )


class GameSelectView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)
        self.add_item(GameSelect())


async def _end_event_autocomplete(interaction: discord.Interaction, current: str):
    if interaction.guild_id is None:
        return []
    now = datetime.now(timezone.utc)
    events = storage.get_events(interaction.guild_id)
    choices = []
    for event_id, event in events.items():
        if event.get("ended"):
            continue
        event_dt = datetime.fromtimestamp(event["timestamp"], tz=timezone.utc)
        if event_dt <= now:
            continue
        label = f"{event['game']} {event['type']} - {event_dt.strftime('%m/%d %I:%M %p UTC')}"
        if current.lower() in label.lower():
            choices.append(app_commands.Choice(name=label[:100], value=event_id))
    return choices[:25]


def register_persistent_views(bot: discord.Client) -> None:
    """Call this once in on_ready so reminder buttons keep working after a
    restart -- mirrors how bot.py handles its own startup state."""
    for guild_id, guild in storage.get_all_guilds().items():
        for event_id, event in guild.get("events", {}).items():
            if not event.get("ended"):
                bot.add_view(ReminderView(int(guild_id), event_id, disabled=event.get("disabled", False)))


def setup(bot: discord.Client, resolve_channel, announce_channel_types) -> None:
    """resolve_channel: bot.py's async (channel_id) -> channel-or-None helper.
    announce_channel_types: bot.py's ANNOUNCE_CHANNEL_TYPES tuple.
    Both are injected rather than imported to avoid a circular import with
    bot.py (which imports this module)."""
    global _resolve_channel, _announce_channel_types
    _resolve_channel = resolve_channel
    _announce_channel_types = announce_channel_types

    @bot.tree.command(name="setcustomschannel", description="Set this channel for custom/tournament announcements")
    @app_commands.guild_only()
    @app_commands.default_permissions(**REQUIRED_PERMS)
    @app_commands.checks.has_permissions(**REQUIRED_PERMS)
    async def setcustomschannel(interaction: discord.Interaction):
        if not isinstance(interaction.channel, _announce_channel_types):
            await interaction.response.send_message(
                "This command only works in a server text channel.", ephemeral=True
            )
            return
        storage.set_customs_channel_id(interaction.guild_id, interaction.channel_id)
        message = f"✅ Customs/tournament announcements will be posted in {interaction.channel.mention}"
        perms = interaction.channel.permissions_for(interaction.guild.me)
        if not (perms.send_messages and perms.embed_links):
            message += (
                "\n⚠️ I'm missing **Send Messages** and/or **Embed Links** in this channel. "
                "Fix that or announcements won't show up."
            )
        if not perms.mention_everyone:
            message += (
                "\n⚠️ I'm missing the **Mention Everyone** permission in this channel, "
                "so announcements can't ping @everyone."
            )
        await interaction.response.send_message(message)

    @bot.tree.command(name="settimezone", description="Set this server's timezone for event times")
    @app_commands.guild_only()
    @app_commands.default_permissions(**REQUIRED_PERMS)
    @app_commands.checks.has_permissions(**REQUIRED_PERMS)
    @app_commands.choices(
        timezone_name=[
            app_commands.Choice(name=label, value=iana)
            for label, iana in TIMEZONE_CHOICES.items()
        ]
    )
    async def settimezone(interaction: discord.Interaction, timezone_name: app_commands.Choice[str]):
        storage.set_timezone(interaction.guild_id, timezone_name.value)
        await interaction.response.send_message(f"✅ Server timezone set to **{timezone_name.name}**.")

    @bot.tree.command(name="create", description="Create a custom game/tournament announcement")
    @app_commands.guild_only()
    @app_commands.default_permissions(**REQUIRED_PERMS)
    @app_commands.checks.has_permissions(**REQUIRED_PERMS)
    async def create(interaction: discord.Interaction):
        await interaction.response.send_message(
            "Which game do you want to make an announcement for?",
            view=GameSelectView(),
            ephemeral=True,
        )

    @bot.tree.command(name="end", description="End/cancel an active customs or tournament announcement")
    @app_commands.guild_only()
    @app_commands.default_permissions(**REQUIRED_PERMS)
    @app_commands.checks.has_permissions(**REQUIRED_PERMS)
    @app_commands.describe(event="The event to end")
    @app_commands.autocomplete(event=_end_event_autocomplete)
    async def end(interaction: discord.Interaction, event: str):
        guild_id = interaction.guild_id
        event_data = storage.get_event(guild_id, event)
        if event_data is None or event_data.get("ended"):
            await interaction.response.send_message(
                "❌ That event doesn't exist or has already ended.", ephemeral=True
            )
            return

        storage.update_event(guild_id, event, ended=True)

        notified = 0
        for user_id in event_data.get("reminding_users", []):
            try:
                user = bot.get_user(user_id) or await bot.fetch_user(user_id)
                await user.send(
                    f"❌ The Marvel Rivals {event_data['type'].lower()} you signed up for has "
                    f"been cancelled/ended."
                )
                notified += 1
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                continue

        await interaction.response.send_message(
            f"✅ Event ended. Notified {notified} user(s) who had a reminder set.", ephemeral=True
        )

    @tasks.loop(seconds=CHECK_INTERVAL_SECONDS)
    async def reminder_loop():
        try:
            await _run_reminder_check_once(bot)
        except Exception:
            logger.exception("reminder_loop cycle failed")

    @reminder_loop.before_loop
    async def before_reminder_loop():
        await bot.wait_until_ready()

    @reminder_loop.error
    async def on_reminder_loop_error(error: BaseException):
        logger.error("reminder_loop crashed: %s", error)
        reminder_loop.restart()

    # Don't start the loop here -- there's no running event loop yet at
    # import time. bot.py starts it from on_ready, same as check_streams.
    bot.customs_reminder_loop = reminder_loop


async def _run_reminder_check_once(bot: discord.Client) -> None:
    now = datetime.now(timezone.utc)
    for guild_id_str, guild in storage.get_all_guilds().items():
        guild_id = int(guild_id_str)
        for event_id, event in guild.get("events", {}).items():
            if event.get("ended"):
                continue
            try:
                await _check_single_event(bot, guild_id, event_id, event, now)
            except Exception:
                logger.exception("[%s] Error processing customs event %s", guild_id, event_id)


async def _check_single_event(bot, guild_id: int, event_id: str, event: dict, now: datetime) -> None:
    event_dt = datetime.fromtimestamp(event["timestamp"], tz=timezone.utc)
    remind_at = event_dt - timedelta(minutes=REMINDER_MINUTES_BEFORE)

    if not event.get("reminder_sent") and now >= remind_at:
        for user_id in event.get("reminding_users", []):
            try:
                user = bot.get_user(user_id) or await bot.fetch_user(user_id)
                await user.send(
                    f"⏰ {REMINDER_MINUTES_BEFORE} minutes until the Marvel Rivals "
                    f"{event['type'].lower()} in the Dojo!\n"
                    f"Room: **{event['room_name']}** | Password: **{event['room_password']}**"
                )
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                continue
        storage.update_event(guild_id, event_id, reminder_sent=True)

    if not event.get("started_announced") and now >= event_dt:
        for user_id in event.get("reminding_users", []):
            try:
                user = bot.get_user(user_id) or await bot.fetch_user(user_id)
                await user.send(
                    f"🔴 The Marvel Rivals {event['type'].lower()} is starting now in the Dojo!\n"
                    f"Room: **{event['room_name']}** | Password: **{event['room_password']}**"
                )
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                continue

        channel = await _resolve_channel(event["channel_id"])
        if channel is not None:
            try:
                reply_to = (
                    discord.MessageReference(
                        message_id=event["message_id"], channel_id=event["channel_id"], fail_if_not_exists=False
                    )
                    if event.get("message_id")
                    else None
                )
                await channel.send(
                    f"🔴 The Marvel Rivals {event['type'].lower()} is starting **now** in the Dojo!\n"
                    f"Room: **{event['room_name']}** | Password: **{event['room_password']}**",
                    reference=reply_to,
                )
            except discord.HTTPException:
                logger.exception(
                    "[%s] Could not post start announcement for event %s", guild_id, event_id
                )
        storage.update_event(guild_id, event_id, started_announced=True)

    if not event.get("disabled") and now >= event_dt:
        channel = await _resolve_channel(event["channel_id"])
        if channel is not None and event.get("message_id"):
            try:
                message = await channel.fetch_message(event["message_id"])
                await message.edit(view=ReminderView(guild_id, event_id, disabled=True))
            except (discord.NotFound, discord.HTTPException):
                logger.exception(
                    "[%s] Could not disable reminder button for event %s", guild_id, event_id
                )
        storage.update_event(guild_id, event_id, disabled=True)
