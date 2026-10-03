"""
Music for the Caterinna Ronin bot: YouTube video links, playlist links, and
plain-text search, played in voice with a now-playing panel.

Ported from the standalone Sarada music bot. bot.py wires it in with
`music.setup(bot)`; the one-time yt-dlp update is `music.update_ytdlp()`.

Needs:
    pip install -r requirements.txt     (discord.py[voice] and yt-dlp)
    ffmpeg installed and on PATH

Optional env vars:
    COOKIES_FILE        path to a cookies.txt if YouTube blocks you
    AUTO_UPDATE_YTDLP   set to 0 to skip the yt-dlp update on start
"""
import asyncio
import importlib.metadata
import logging
import os
import random
import subprocess
import sys
from collections import deque
from collections.abc import Coroutine
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import parse_qs, urlparse

import discord
import yt_dlp
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

if TYPE_CHECKING:
    # _Params and _InfoDict only exist in yt-dlp's bundled type stubs, never
    # at runtime, so they must be imported for the checker only.
    from yt_dlp import _Params
    from yt_dlp.extractor.common import _InfoDict

BASE_DIR = Path(__file__).resolve().parent
# bot.py imports this module before it calls load_dotenv(), and COOKIES_FILE is
# read at import time below, so load the .env here too (it's a no-op to repeat).
load_dotenv(BASE_DIR / ".env")

log = logging.getLogger("music")

MAX_PLAYLIST = 1000      # cap on tracks pulled from one playlist
IDLE_SECONDS = 300       # leave voice after this long with nothing playing
DEFAULT_VOLUME = 0.5
COOKIES_FILE = os.getenv("COOKIES_FILE")
if COOKIES_FILE and not Path(COOKIES_FILE).is_absolute():
    COOKIES_FILE = str(BASE_DIR / COOKIES_FILE)

YOUTUBE_HOSTS = {"youtube.com", "youtu.be"}
BAD_TITLES = {"[Private video]", "[Deleted video]"}
FFMPEG_BEFORE = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"

# Titles and display names come from YouTube and Discord, so they're untrusted:
# a track called "@everyone" must not be able to ping a server. Caterinna's
# client allows @everyone for live announcements, so every music message opts
# out explicitly instead of relying on a client-wide default.
NO_MENTIONS = discord.AllowedMentions.none()


class UserError(Exception):
    """An error whose message is safe to show to the user."""


def _ytdlp_version() -> str | None:
    try:
        return importlib.metadata.version("yt-dlp")
    except importlib.metadata.PackageNotFoundError:
        return None


def update_ytdlp() -> None:
    """Best-effort yt-dlp update. Never fatal: on any failure we just keep
    running with whatever version is already installed."""
    if os.getenv("AUTO_UPDATE_YTDLP") == "0":
        log.info("yt-dlp auto-update is disabled (AUTO_UPDATE_YTDLP=0)")
        return
    before = _ytdlp_version()
    # sys.executable keeps the install inside the environment this bot runs in,
    # and CREATE_NO_WINDOW stops a console window flashing on Windows.
    try:
        # yt-dlp[default] rather than plain yt-dlp: the extras pull in the
        # JS runtime support yt-dlp needs to extract YouTube formats.
        proc = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-U", "yt-dlp[default]",
             "--disable-pip-version-check", "-q"],
            capture_output=True,
            text=True,
            timeout=120,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        log.warning("yt-dlp update timed out after 120s; continuing with the installed version")
        return
    except Exception as e:
        log.warning("yt-dlp update failed: %r; continuing with the installed version", e)
        return
    if proc.returncode != 0:
        log.warning(
            "yt-dlp update failed (pip exit %d): %s",
            proc.returncode,
            (proc.stderr or "").strip()[-500:],
        )
        return
    after = _ytdlp_version()
    if after is None:
        log.warning("yt-dlp version is still unreadable after the update attempt")
    elif before is not None and before != after:
        log.info("yt-dlp updated: %s -> %s", before, after)
    else:
        log.info("yt-dlp is up to date (%s)", after)


# ───────────────────────────── track + link handling ─────────────────────────
@dataclass
class Track:
    title: str
    url: str                      # watch URL; the stream URL is resolved at play time
    duration: int | None = None
    uploader: str | None = None
    requester: str | None = None

    @property
    def thumb(self) -> str | None:
        u = urlparse(self.url)
        vid = parse_qs(u.query).get("v", [None])[0]
        if not vid and (u.netloc.endswith("youtu.be") or u.path.startswith(("/shorts/", "/live/"))):
            segments = [s for s in u.path.split("/") if s]
            vid = segments[-1] if segments else None
        return f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg" if vid else None


def fmt_duration(sec: int | None) -> str:
    if not sec:
        return "?:??"
    m, s = divmod(int(sec), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def classify(text: str) -> str:
    """Returns 'search', 'video', 'playlist' or 'unsupported'."""
    if not text.startswith(("http://", "https://")):
        return "search"
    u = urlparse(text)
    host = u.netloc.lower()
    for prefix in ("www.", "m.", "music."):
        host = host.removeprefix(prefix)
    if host not in YOUTUBE_HOSTS:
        return "unsupported"
    q = parse_qs(u.query)
    if host == "youtu.be" or "v" in q or u.path.startswith(("/shorts/", "/live/")):
        return "video"          # includes watch?v=X&list=Y (we queue just the video)
    if "list" in q:
        return "playlist"
    return "unsupported"


def list_id(text: str) -> str | None:
    return parse_qs(urlparse(text).query).get("list", [None])[0]


# ───────────────────────────── yt-dlp helpers (blocking) ─────────────────────
class YDLLogger:
    """Feeds yt-dlp's own messages into our log files, in place of its console."""

    def debug(self, msg: str) -> None:
        pass

    def info(self, msg: str) -> None:
        pass

    def warning(self, msg: str) -> None:
        log.warning("yt-dlp: %s", msg)

    def error(self, msg: str) -> None:
        log.error("yt-dlp: %s", msg)


def _opts(**extra: Any) -> "_Params":
    # no "no_warnings": dropping it is what lets yt-dlp's warnings reach YDLLogger.
    # socket_timeout/retries keep a stalled request from pinning the worker
    # thread that asyncio.to_thread is blocked on, forever.
    o: dict[str, Any] = {
        "quiet": True,
        "skip_download": True,
        "socket_timeout": 20,
        "retries": 3,
        "logger": YDLLogger(),
    }
    if COOKIES_FILE:
        o["cookiefile"] = COOKIES_FILE
    o.update(extra)
    # _Params is a total=False TypedDict, so a plain dict is not assignable to
    # it; every key set here is a documented yt-dlp option.
    return cast("_Params", o)


def _watch_url(entry: "_InfoDict") -> str:
    return entry.get("url") or f"https://www.youtube.com/watch?v={entry['id']}"


def _track_from_entry(entry: "_InfoDict") -> Track:
    """Build a Track from a flat (extract_flat) entry, as playlists and search give."""
    return Track(
        entry.get("title") or "Unknown",
        _watch_url(entry),
        entry.get("duration"),
        entry.get("channel") or entry.get("uploader"),
    )


def fetch_video(url: str) -> Track:
    with yt_dlp.YoutubeDL(_opts(noplaylist=True, format="bestaudio/best")) as ydl:
        info = ydl.extract_info(url, download=False)
    # _InfoDict declares "title" optional, so the checker rejects a [key] access;
    # a single-video extract always has one, and this raises the same KeyError.
    title = info.get("title")
    if title is None:
        raise KeyError("title")
    return Track(
        title,
        info.get("webpage_url") or url,
        info.get("duration"),
        info.get("channel") or info.get("uploader"),
    )


def fetch_playlist(url: str) -> tuple[str, list[Track]]:
    with yt_dlp.YoutubeDL(_opts(extract_flat="in_playlist", playlistend=MAX_PLAYLIST)) as ydl:
        info = ydl.extract_info(url, download=False)
    tracks = [
        _track_from_entry(e)
        for e in (info.get("entries") or [])
        if e and e.get("title") not in BAD_TITLES
    ]
    return info.get("title") or "playlist", tracks


def search_first(query: str) -> Track | None:
    with yt_dlp.YoutubeDL(_opts(extract_flat=True)) as ydl:
        info = ydl.extract_info(f"ytsearch1:{query}", download=False)
    entries = [e for e in (info.get("entries") or []) if e]
    if not entries:
        return None
    return _track_from_entry(entries[0])


def resolve_stream(watch_url: str) -> str:
    with yt_dlp.YoutubeDL(_opts(format="bestaudio/best", noplaylist=True)) as ydl:
        info = ydl.extract_info(watch_url, download=False)
    # As above: "url" is optional in _InfoDict, but a resolved bestaudio has one.
    url = info.get("url")
    if url is None:
        raise KeyError("url")
    return url


# asyncio keeps only a weak reference to a running task, so a fire-and-forget
# create_task can be garbage-collected before it ever runs. Parking them here
# until they finish keeps them alive. Never iterate this.
_BACKGROUND_TASKS: set[asyncio.Task[Any]] = set()


def spawn(coro: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
    """Start a coroutine in the background and hold a reference until it's done."""
    task = asyncio.create_task(coro)
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    return task


# ───────────────────────────── per-guild player ──────────────────────────────
class GuildPlayer:
    def __init__(self):
        self.queue: deque[Track] = deque()
        self.history: deque[Track] = deque(maxlen=50)
        self.current: Track | None = None
        self.vc: discord.VoiceClient | None = None
        self.text_channel: discord.abc.Messageable | None = None
        self.panel: discord.Message | None = None
        self.volume = DEFAULT_VOLUME
        self._lock = asyncio.Lock()
        self._idle_task: asyncio.Task | None = None
        self._suppress_history = False

    def is_active(self) -> bool:
        return bool(self.vc and (self.vc.is_playing() or self.vc.is_paused()))

    async def add(self, tracks: list[Track]):
        self._cancel_idle()
        was_active = self.is_active()
        self.queue.extend(tracks)
        await self.play_next()
        if was_active:
            await self.refresh_panel()

    async def play_next(self):
        async with self._lock:
            if self.is_active():
                return
            # whatever we are replacing counts as finished, unless a /previous
            # put it back on the queue on purpose.
            finished = self.current
            suppress, self._suppress_history = self._suppress_history, False
            while self.queue:
                if not self.vc or not self.vc.is_connected():
                    break
                track = self.queue.popleft()
                try:
                    stream = await asyncio.to_thread(resolve_stream, track.url)
                except Exception as e:
                    log.warning("resolve failed for %s: %r", track.title, e, exc_info=True)
                    await self._say(f"Couldn't play **{track.title}**, skipping.")
                    continue
                source = discord.PCMVolumeTransformer(
                    discord.FFmpegPCMAudio(stream, before_options=FFMPEG_BEFORE, options="-vn"),
                    volume=self.volume,
                )
                loop = asyncio.get_running_loop()

                def after(err, loop=loop):
                    if err:
                        log.error("player error: %r", err)
                    # already on the loop thread here, so spawn is safe
                    loop.call_soon_threadsafe(lambda: spawn(self.play_next()))

                if finished and not suppress:
                    self.history.append(finished)
                self.current = track
                log.info("Playing %s (%s)", track.title, track.url)
                self.vc.play(source, after=after)
                await self._post_panel()
                return
            # queue exhausted (or not connected)
            if finished and not suppress:
                self.history.append(finished)
            self.current = None
            await self._end_panel()
            self.schedule_idle()

    def previous(self) -> bool:
        if not self.vc:
            return False
        if self.history:
            prev = self.history.pop()
        elif self.current:
            prev = None                # nothing to go back to: restart this one
        else:
            return False
        if self.current:
            self.queue.appendleft(self.current)
        if prev:
            self.queue.appendleft(prev)
        self._suppress_history = True
        if self.is_active():
            self.vc.stop()            # triggers `after` → play_next
        else:
            spawn(self.play_next())
        return True

    async def _idle_disconnect(self):
        await asyncio.sleep(IDLE_SECONDS)
        if not self.is_active() and self.vc and self.vc.is_connected():
            log.info("Idle for %ds, leaving %s", IDLE_SECONDS, self.vc.channel)
            await self.vc.disconnect()

    def _cancel_idle(self):
        if self._idle_task:
            self._idle_task.cancel()
            self._idle_task = None

    def schedule_idle(self):
        """Arm the idle disconnect, but only when there is genuinely nothing to wait for.

        A paused track counts as active, so pausing never starts the timer. Used at
        the end of the queue and when a load fails, since either way we end up
        connected with nothing playing.
        """
        self._cancel_idle()
        if self.is_active() or self.queue:
            return
        if self.vc and self.vc.is_connected():
            self._idle_task = spawn(self._idle_disconnect())

    async def _say(self, text: str):
        if self.text_channel:
            try:
                await self.text_channel.send(text, allowed_mentions=NO_MENTIONS)
            except discord.HTTPException:
                pass

    def skip(self):
        if self.vc and self.is_active():
            self.vc.stop()          # triggers `after` → play_next

    def clear(self):
        self.queue.clear()

    def shuffle(self):
        items = list(self.queue)
        random.shuffle(items)
        self.queue = deque(items)

    def set_volume(self, value: float):
        self.volume = value
        if self.vc and isinstance(self.vc.source, discord.PCMVolumeTransformer):
            self.vc.source.volume = value

    def build_embed(self, paused: bool = False) -> discord.Embed:
        track = self.current
        embed = discord.Embed(
            title=track.title[:256] if track else "Nothing is playing",
            colour=0x8A94A6,
        )
        if track:
            embed.url = track.url
        embed.set_author(name="Paused" if paused else "Now playing")
        bits = []
        if track and track.uploader:
            bits.append(track.uploader)
        if track and track.duration:
            bits.append(fmt_duration(track.duration))
        if bits:
            embed.description = " · ".join(bits)
        if track and (thumb := track.thumb):
            # set_thumbnail() is the same image, just pinned smaller in a corner.
            embed.set_image(url=thumb)
        embed.add_field(
            name="Up next",
            value=self.queue[0].title[:100] if self.queue else "nothing",
            inline=True,
        )
        embed.add_field(name="In queue", value=str(len(self.queue)), inline=True)
        if track and track.requester:
            embed.set_footer(text=f"Requested by {track.requester}")
        return embed

    async def _post_panel(self) -> bool:
        """Post a fresh panel, replacing the old one. False if we could not."""
        if not self.text_channel or not self.current:
            return False
        old = self.panel
        try:
            self.panel = await self.text_channel.send(
                embed=self.build_embed(), view=PlayerView(self),
                allowed_mentions=NO_MENTIONS,
            )
        except discord.HTTPException as e:
            log.warning("could not post the now-playing panel: %r", e)
            return False
        if old:
            try:
                await old.delete()
            except discord.HTTPException:
                pass
        return True

    async def refresh_panel(self):
        if not self.panel or not self.current:
            return
        paused = bool(self.vc and self.vc.is_paused())
        try:
            # only the embed is passed, so the buttons stay as they are
            await self.panel.edit(embed=self.build_embed(paused=paused))
        except discord.HTTPException as e:
            log.debug("could not refresh the now-playing panel: %r", e)

    async def _end_panel(self):
        if not self.panel:
            return
        embed = discord.Embed(title="Queue finished.", colour=0x8A94A6)
        try:
            await self.panel.edit(embed=embed, view=None)
        except discord.HTTPException as e:
            log.debug("could not clear the now-playing panel: %r", e)
        self.panel = None


players: dict[int, GuildPlayer] = {}


def get_player(guild_id: int) -> GuildPlayer:
    return players.setdefault(guild_id, GuildPlayer())


def player_for(interaction: discord.Interaction) -> tuple[discord.Guild, GuildPlayer]:
    """Every command is guild_only, so a guild is always present at runtime."""
    guild = interaction.guild
    assert guild is not None, "guild_only command invoked outside a guild"
    return guild, get_player(guild.id)


async def require_same_channel(interaction: discord.Interaction, player: GuildPlayer) -> bool:
    """Block someone who isn't in the bot's voice channel from driving playback.

    Returns True when the caller may carry on. If the bot isn't in voice at all
    there is nothing to protect, so the command runs and reports that there is
    nothing playing. /play and /playlist don't use this: they connect or move
    the bot through ensure_voice instead.
    """
    vc = player.vc
    if not isinstance(vc, discord.VoiceClient):
        return True
    user = interaction.user
    same_channel = (
        isinstance(user, discord.Member)
        and user.voice is not None
        and user.voice.channel is not None
        and user.voice.channel == vc.channel
    )
    if not same_channel:
        await interaction.response.send_message("Join my voice channel first.", ephemeral=True)
        return False
    return True


async def ensure_voice(interaction: discord.Interaction) -> GuildPlayer | None:
    user = interaction.user
    voice = user.voice if isinstance(user, discord.Member) else None
    voice_channel = voice.channel if voice else None
    if voice_channel is None:
        await interaction.followup.send("Join a voice channel first.")
        return None
    guild, player = player_for(interaction)
    text = interaction.channel
    if isinstance(text, discord.abc.Messageable):
        player.text_channel = text
    vc = guild.voice_client
    if isinstance(vc, discord.VoiceClient):
        if vc.channel != voice_channel:
            # don't drag the bot out of a channel others are listening to
            if player.is_active():
                where = vc.channel.name if vc.channel else "another channel"
                await interaction.followup.send(f"I'm already playing in {where}.")
                return None
            await vc.move_to(voice_channel)
            log.info("Moved to voice channel %s", voice_channel)
    else:
        # connect() is annotated as returning VoiceProtocol, but it always builds
        # a VoiceClient (VoiceChannel._get_voice_client_type).
        vc = await voice_channel.connect()
        assert isinstance(vc, discord.VoiceClient)
        log.info("Joined voice channel %s", voice_channel)
    player.vc = vc
    return player


# ───────────────────────── input → queue (the LLM will call this later) ──────
async def load_and_queue(
    player: GuildPlayer,
    query: str,
    force_playlist: bool = False,
    requester: str | None = None,
) -> str:
    """Resolve a link or search text and queue it. Returns a message for the user."""
    query = query.strip()
    kind = classify(query)
    if kind == "unsupported":
        raise UserError("I only handle YouTube video links, playlist links, or plain search text.")

    if force_playlist:
        lid = list_id(query) if kind != "search" else None
        if not lid:
            raise UserError("That link doesn't contain a playlist.")
        kind, query = "playlist", f"https://www.youtube.com/playlist?list={lid}"

    if kind == "playlist":
        title, tracks = await asyncio.to_thread(fetch_playlist, query)
        if not tracks:
            raise UserError("I couldn't find any playable tracks in that playlist.")
        for t in tracks:
            t.requester = requester
        await player.add(tracks)
        note = f" (capped at {MAX_PLAYLIST})" if len(tracks) >= MAX_PLAYLIST else ""
        log.info("Queued %d tracks from playlist %s", len(tracks), title)
        return f"Queued **{len(tracks)}** tracks from **{title}**{note}"

    if kind == "video":
        track = await asyncio.to_thread(fetch_video, query)
        track.requester = requester
        await player.add([track])
        log.info("Queued video %s (%s)", track.title, track.url)
        msg = f"Queued **{track.title}** [{fmt_duration(track.duration)}]"
        if list_id(query):
            msg += "\n(That link also had a playlist. Use `/playlist` to queue all of it.)"
        return msg

    track = await asyncio.to_thread(search_first, query)
    if not track:
        raise UserError("No results found.")
    track.requester = requester
    await player.add([track])
    log.info("Queued search result %s (%s)", track.title, track.url)
    return f"Queued **{track.title}** [{fmt_duration(track.duration)}]"


# ───────────────────────────── Discord wiring ────────────────────────────────
async def on_voice_state_update(member, before, after):
    vc = member.guild.voice_client
    if vc and vc.channel and all(m.bot for m in vc.channel.members):
        log.info("Everyone left %s, disconnecting", vc.channel)
        get_player(member.guild.id).clear()
        await vc.disconnect()


class PlayerView(discord.ui.View):
    """The transport controls that ride along with the now-playing embed."""

    def __init__(self, player: GuildPlayer):
        super().__init__(timeout=None)
        self.player = player

    async def _guard(self, interaction: discord.Interaction) -> bool:
        return await require_same_channel(interaction, self.player)

    @discord.ui.button(emoji="⏮️", label="Previous", style=discord.ButtonStyle.secondary)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard(interaction):
            return
        if self.player.previous():
            await interaction.response.defer()
        else:
            await interaction.response.send_message("Nothing to go back to.", ephemeral=True)

    @discord.ui.button(emoji="⏯️", label="Play / Pause", style=discord.ButtonStyle.primary)
    async def toggle_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard(interaction):
            return
        vc = self.player.vc
        if not isinstance(vc, discord.VoiceClient):
            return await interaction.response.send_message("Nothing is playing.", ephemeral=True)
        paused = False
        if vc.is_playing():
            vc.pause()
            paused = True
        elif vc.is_paused():
            vc.resume()
        await interaction.response.edit_message(embed=self.player.build_embed(paused=paused), view=self)

    @discord.ui.button(emoji="⏭️", label="Next", style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard(interaction):
            return
        await interaction.response.defer()
        self.player.skip()

    @discord.ui.button(emoji="⏹️", label="Stop", style=discord.ButtonStyle.danger)
    async def stop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard(interaction):
            return
        await interaction.response.defer()
        self.player.clear()
        self.player.skip()


async def run_load(interaction: discord.Interaction, query: str, force_playlist=False):
    await interaction.response.defer()
    player = await ensure_voice(interaction)
    if not player:
        return
    try:
        msg = await load_and_queue(
            player, query, force_playlist, interaction.user.display_name
        )
    except UserError as e:
        msg = str(e)
        # the bot is already connected but nothing will play: don't sit there
        # until everyone else leaves.
        player.schedule_idle()
    except Exception as e:
        log.error("load error: %r", e, exc_info=True)
        msg = "Couldn't load that. It may be private, age-restricted, or blocked."
        player.schedule_idle()
    await interaction.followup.send(msg, allowed_mentions=NO_MENTIONS)


@app_commands.command(name="play", description="Play a YouTube video or playlist link, or search by name")
@app_commands.describe(query="YouTube link or search terms")
@app_commands.guild_only()
async def play(interaction: discord.Interaction, query: str):
    await run_load(interaction, query)


@app_commands.command(
    name="playlist",
    description="Queue a whole playlist (also works on video links that include a list)",
)
@app_commands.describe(url="YouTube playlist link")
@app_commands.guild_only()
async def playlist(interaction: discord.Interaction, url: str):
    await run_load(interaction, url, force_playlist=True)


@app_commands.command(name="skip", description="Skip the current track")
@app_commands.guild_only()
async def skip(interaction: discord.Interaction):
    _, p = player_for(interaction)
    if not await require_same_channel(interaction, p):
        return
    if not p.is_active() or not p.current:
        return await interaction.response.send_message("Nothing is playing.")
    title = p.current.title
    p.skip()
    await interaction.response.send_message(f"Skipped **{title}**", allowed_mentions=NO_MENTIONS)


@app_commands.command(name="previous", description="Play the previous track again")
@app_commands.guild_only()
async def previous(interaction: discord.Interaction):
    _, p = player_for(interaction)
    if not await require_same_channel(interaction, p):
        return
    if p.previous():
        await interaction.response.send_message("Going back.")
    else:
        await interaction.response.send_message("Nothing to go back to.")


@app_commands.command(name="pause", description="Pause playback")
@app_commands.guild_only()
async def pause(interaction: discord.Interaction):
    guild, p = player_for(interaction)
    if not await require_same_channel(interaction, p):
        return
    vc = guild.voice_client
    if isinstance(vc, discord.VoiceClient) and vc.is_playing():
        vc.pause()
        await p.refresh_panel()
        return await interaction.response.send_message("Paused.")
    await interaction.response.send_message("Nothing is playing.")


@app_commands.command(name="resume", description="Resume playback")
@app_commands.guild_only()
async def resume(interaction: discord.Interaction):
    guild, p = player_for(interaction)
    if not await require_same_channel(interaction, p):
        return
    vc = guild.voice_client
    if isinstance(vc, discord.VoiceClient) and vc.is_paused():
        vc.resume()
        await p.refresh_panel()
        return await interaction.response.send_message("Resumed.")
    await interaction.response.send_message("Nothing is paused.")


@app_commands.command(name="stop", description="Stop playback and clear the queue")
@app_commands.guild_only()
async def stop(interaction: discord.Interaction):
    _, p = player_for(interaction)
    if not await require_same_channel(interaction, p):
        return
    p.clear()
    p.skip()
    await interaction.response.send_message("Stopped and cleared the queue.")


@app_commands.command(name="queue", description="Show what's playing and what's next")
@app_commands.guild_only()
async def queue(interaction: discord.Interaction):
    _, p = player_for(interaction)
    now = f"{p.current.title} [{fmt_duration(p.current.duration)}]" if p.current else "nothing"
    items = list(p.queue)
    lines = [f"{i + 1}. {t.title} [{fmt_duration(t.duration)}]" for i, t in enumerate(items[:10])]
    if len(items) > 10:
        lines.append(f"...and {len(items) - 10} more")
    await interaction.response.send_message(
        f"**Now playing:** {now}\n**Up next:**\n" + ("\n".join(lines) or "empty"),
        allowed_mentions=NO_MENTIONS,
    )


@app_commands.command(name="shuffle", description="Shuffle the queue")
@app_commands.guild_only()
async def shuffle(interaction: discord.Interaction):
    _, p = player_for(interaction)
    if not await require_same_channel(interaction, p):
        return
    p.shuffle()
    await p.refresh_panel()
    await interaction.response.send_message("Shuffled.")


@app_commands.command(name="nowplaying", description="Show the now-playing panel again")
@app_commands.guild_only()
async def nowplaying(interaction: discord.Interaction):
    _, p = player_for(interaction)
    if not p.current:
        return await interaction.response.send_message("Nothing is playing.", ephemeral=True)
    if isinstance(interaction.channel, discord.abc.Messageable):
        p.text_channel = interaction.channel
    await interaction.response.defer(ephemeral=True)
    if await p._post_panel():
        await interaction.followup.send("Panel refreshed.", ephemeral=True)
    else:
        await interaction.followup.send("Couldn't post the panel.", ephemeral=True)


@app_commands.command(name="volume", description="Set volume (0-100)")
@app_commands.guild_only()
async def volume(interaction: discord.Interaction, level: app_commands.Range[int, 0, 100]):
    _, p = player_for(interaction)
    if not await require_same_channel(interaction, p):
        return
    p.set_volume(level / 100)
    await interaction.response.send_message(f"Volume set to {level}%")


@app_commands.command(name="leave", description="Clear the queue and leave voice")
@app_commands.guild_only()
async def leave(interaction: discord.Interaction):
    guild, p = player_for(interaction)
    if not await require_same_channel(interaction, p):
        return
    p.clear()
    vc = guild.voice_client
    if isinstance(vc, discord.VoiceClient):
        await vc.disconnect()
    await interaction.response.send_message("Bye.")


_COMMANDS = (
    play, playlist, skip, previous, pause, resume,
    stop, queue, shuffle, nowplaying, volume, leave,
)


def setup(bot: commands.Bot) -> None:
    """Register the music commands and the empty-channel listener on the bot.

    Call this before the command tree is synced (bot.py's setup_hook syncs, so
    calling it at import time, next to customs.setup, is early enough).
    """
    for command in _COMMANDS:
        bot.tree.add_command(command)
    bot.add_listener(on_voice_state_update, "on_voice_state_update")
    log.info("Music commands registered (%d)", len(_COMMANDS))
