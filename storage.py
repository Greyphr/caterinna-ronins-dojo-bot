import json
import logging
import os
import tempfile
import time
from typing import Any, Dict, List, Optional, Tuple

DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.json")

logger = logging.getLogger("storage")

# data.json layout:
# {
#   "guilds": {
#     "<guild_id>": {
#       "channel_id": 123,
#       "streamers": {
#         "<twitch_login>": {"added_by": 1, "is_live": false,
#                            "display_name": "...", "profile_image_url": "...",
#                            "stream_id": "1234567890"}  # optional: id of the announced stream
#       },
#       "customs_channel_id": 456,
#       "timezone": "Africa/Lagos",  # IANA name; set via /settimezone
#       "events": {
#         "<event_id>": {
#           "game": "Marvel Rivals", "type": "Tournament" | "Customs",
#           "banner_url": "...", "timestamp": 1735130400,
#           "room_name": "...", "room_password": "...", "created_by": 1,
#           "channel_id": 456, "message_id": 789,
#           "ended": false, "reminder_sent": false, "disabled": false,
#           "reminding_users": [111, 222]
#         }
#       }
#     }
#   }
# }
#
# Older versions stored "channel_id" and "streamers" at the top level (one
# channel for the whole bot). The bot migrates that into the right guild on
# startup -- see migrate_legacy().
#
# "stream_id" is optional and only exists once a stream has been announced; old
# records without it keep loading fine. Likewise, "customs_channel_id",
# "timezone" and "events" are optional and backfilled with defaults by
# _guild() for any guild record that predates the customs feature.


def _load() -> Dict[str, Any]:
    if not os.path.exists(DATA_FILE):
        return {"guilds": {}}
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            raw = f.read()
        if not raw.strip():
            raise ValueError("data.json is empty")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("data.json is not a JSON object")
    except (ValueError, json.JSONDecodeError) as exc:
        _quarantine_corrupt_data(exc)
        return {"guilds": {}}
    data.setdefault("guilds", {})
    return data


def _quarantine_corrupt_data(exc: Exception) -> None:
    """Move a broken data.json aside (never delete it) so the bot keeps
    running with an empty dataset instead of crashing forever."""
    directory = os.path.dirname(DATA_FILE)
    corrupt_path = os.path.join(
        directory, f"data.json.corrupt-{time.strftime('%Y%m%d-%H%M%S')}"
    )
    try:
        os.replace(DATA_FILE, corrupt_path)
    except OSError:
        logger.exception("Could not move %s aside for quarantine", DATA_FILE)
        corrupt_path = DATA_FILE
    logger.error(
        "%s is empty or invalid (%s); moved it to %s and continuing with empty data.",
        DATA_FILE,
        exc,
        corrupt_path,
    )


def _save(data: Dict[str, Any]) -> None:
    # Write to a temp file then swap it in, so a crash mid-write can't
    # leave a half-written data.json behind.
    directory = os.path.dirname(DATA_FILE)
    fd, tmp_path = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        _replace_with_retry(tmp_path)
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


def _replace_with_retry(src: str) -> None:
    """Swap the temp file in, retrying a few times if Windows antivirus or
    sync tools (OneDrive) hold a transient lock on data.json."""
    for attempt in range(5):
        try:
            os.replace(src, DATA_FILE)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.2)


def _guild(data: Dict[str, Any], guild_id: int) -> Dict[str, Any]:
    """Get (creating if needed) the settings block for a guild. Also backfills
    fields added after a guild record was first created (customs_channel_id,
    timezone, events), so old records keep working without a migration step."""
    guild = data["guilds"].setdefault(
        str(guild_id), {"channel_id": None, "streamers": {}}
    )
    guild.setdefault("customs_channel_id", None)
    guild.setdefault("timezone", None)
    guild.setdefault("events", {})
    return guild


# ---------- per-guild settings ----------

def get_all_guilds() -> Dict[str, Dict[str, Any]]:
    return _load()["guilds"]


def get_channel_id(guild_id: int) -> Optional[int]:
    return _load()["guilds"].get(str(guild_id), {}).get("channel_id")


def set_channel_id(guild_id: int, channel_id: int) -> None:
    data = _load()
    _guild(data, guild_id)["channel_id"] = channel_id
    _save(data)


def remove_guild(guild_id: int) -> None:
    """Forget a server (used when the bot is removed from it)."""
    data = _load()
    if data["guilds"].pop(str(guild_id), None) is not None:
        _save(data)


# ---------- per-guild streamers ----------

def get_streamers(guild_id: int) -> Dict[str, Any]:
    return _load()["guilds"].get(str(guild_id), {}).get("streamers", {})


def add_streamer(
    guild_id: int,
    username: str,
    added_by: int,
    display_name: str = "",
    profile_image_url: str = "",
) -> bool:
    data = _load()
    guild = _guild(data, guild_id)
    username = username.lower()
    if username in guild["streamers"]:
        return False
    guild["streamers"][username] = {
        "added_by": added_by,
        "is_live": False,
        "display_name": display_name or username,
        "profile_image_url": profile_image_url,
    }
    _save(data)
    return True


def remove_streamer(guild_id: int, username: str) -> bool:
    data = _load()
    guild = data["guilds"].get(str(guild_id))
    username = username.lower()
    if guild and username in guild["streamers"]:
        del guild["streamers"][username]
        _save(data)
        return True
    return False


def set_live_status(
    guild_id: int,
    username: str,
    is_live: bool,
    stream_id: Optional[str] = None,
) -> None:
    """Set the stored is_live flag and, optionally, the id of the currently
    announced stream. is_live=True with a stream_id stores it; with no
    stream_id the existing id (if any) is kept; is_live=False drops the id.
    No file write happens when nothing changes."""
    data = _load()
    guild = data["guilds"].get(str(guild_id))
    username = username.lower()
    if guild and username in guild["streamers"]:
        record = guild["streamers"][username]
        changed = False
        if record.get("is_live") != is_live:
            record["is_live"] = is_live
            changed = True
        if is_live:
            if stream_id is not None and record.get("stream_id") != stream_id:
                record["stream_id"] = stream_id
                changed = True
        elif "stream_id" in record:
            del record["stream_id"]
            changed = True
        if changed:
            _save(data)


def bump_missed_checks(guild_id: int, username: str) -> int:
    """Count one more offline check for a streamer; returns the new count."""
    data = _load()
    guild = data["guilds"].get(str(guild_id))
    username = username.lower()
    if guild and username in guild["streamers"]:
        record = guild["streamers"][username]
        record["missed_checks"] = record.get("missed_checks", 0) + 1
        _save(data)
        return record["missed_checks"]
    return 0


def reset_missed_checks(guild_id: int, username: str) -> None:
    data = _load()
    guild = data["guilds"].get(str(guild_id))
    username = username.lower()
    if guild and username in guild["streamers"]:
        record = guild["streamers"][username]
        if not record.get("missed_checks"):
            return
        record["missed_checks"] = 0
        _save(data)


def get_incomplete_streamers() -> List[Tuple[int, str]]:
    """Streamers whose records lack display info: list of (guild_id, login)."""
    out: List[Tuple[int, str]] = []
    data = _load()
    for guild_id, guild in data.get("guilds", {}).items():
        for username, info in (guild.get("streamers") or {}).items():
            if not info.get("display_name") or not info.get("profile_image_url"):
                out.append((int(guild_id), username))
    return out


def update_streamer_info(
    guild_id: int,
    username: str,
    *,
    display_name: str = "",
    profile_image_url: str = "",
) -> None:
    """Fill in display info on an existing streamer record (backfill)."""
    data = _load()
    guild = data["guilds"].get(str(guild_id))
    username = username.lower()
    if guild and username in guild["streamers"]:
        record = guild["streamers"][username]
        changed = False
        if display_name and record.get("display_name") != display_name:
            record["display_name"] = display_name
            changed = True
        if profile_image_url and record.get("profile_image_url") != profile_image_url:
            record["profile_image_url"] = profile_image_url
            changed = True
        if changed:
            _save(data)


# ---------- per-guild customs/tournament announcements ----------

def get_customs_channel_id(guild_id: int) -> Optional[int]:
    return _load()["guilds"].get(str(guild_id), {}).get("customs_channel_id")


def set_customs_channel_id(guild_id: int, channel_id: int) -> None:
    data = _load()
    _guild(data, guild_id)["customs_channel_id"] = channel_id
    _save(data)


def get_timezone(guild_id: int) -> Optional[str]:
    return _load()["guilds"].get(str(guild_id), {}).get("timezone")


def set_timezone(guild_id: int, tz_name: str) -> None:
    data = _load()
    _guild(data, guild_id)["timezone"] = tz_name
    _save(data)


def get_events(guild_id: int) -> Dict[str, Any]:
    return _load()["guilds"].get(str(guild_id), {}).get("events", {})


def get_event(guild_id: int, event_id: str) -> Optional[Dict[str, Any]]:
    return _load()["guilds"].get(str(guild_id), {}).get("events", {}).get(event_id)


def create_event(guild_id: int, event_id: str, event: Dict[str, Any]) -> None:
    data = _load()
    _guild(data, guild_id)["events"][event_id] = event
    _save(data)


def update_event(guild_id: int, event_id: str, **fields) -> None:
    data = _load()
    guild = data["guilds"].get(str(guild_id))
    if guild and event_id in guild.get("events", {}):
        guild["events"][event_id].update(fields)
        _save(data)


def add_reminder_user(guild_id: int, event_id: str, user_id: int) -> bool:
    """Returns True if the user was newly added, False if already reminding
    or the event/guild doesn't exist."""
    data = _load()
    guild = data["guilds"].get(str(guild_id))
    if not guild:
        return False
    event = guild.get("events", {}).get(event_id)
    if event is None:
        return False
    if user_id in event["reminding_users"]:
        return False
    event["reminding_users"].append(user_id)
    _save(data)
    return True


# ---------- migration from the old single-channel format ----------

def get_legacy_channel_id() -> Optional[int]:
    """The old top-level channel_id, if data.json is still in the old format."""
    return _load().get("channel_id")


def has_legacy_data() -> bool:
    data = _load()
    return "channel_id" in data or "streamers" in data


def migrate_legacy(guild_id: int) -> None:
    """Move the old top-level channel/streamers into the given guild."""
    data = _load()
    guild = _guild(data, guild_id)

    legacy_channel = data.pop("channel_id", None)
    legacy_streamers = data.pop("streamers", {}) or {}

    if legacy_channel and not guild.get("channel_id"):
        guild["channel_id"] = legacy_channel
    for name, info in legacy_streamers.items():
        guild["streamers"].setdefault(name.lower(), info)

    _save(data)
