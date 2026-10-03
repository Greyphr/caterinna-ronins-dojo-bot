r"""Tests for the pure helpers in music.py.

Only the functions that decide things from a string: no network, no Discord
connection. Importing music is safe for that, since nothing talks to Discord
or pip until bot.py calls music.setup() / music.update_ytdlp().

Run with:  python -m pytest
"""
import pytest

from music import Track, classify, fmt_duration, list_id

PLAYLIST = "PLrAXtmRdnEQy6nuLMfO6uJhFhrqcTZJg"


def track(url: str) -> Track:
    return Track(title="test", url=url)


# ────────────────────────────────── classify ────────────────────────────────
@pytest.mark.parametrize("text", [
    "never gonna give you up",
    "Rick Astley - Never Gonna Give You Up",
    "",
    "   ",
    "youtube.com/watch?v=abc123",        # pasted without a scheme
    "https:/typo.com",                   # one slash
    "ftp://youtube.com/watch?v=abc123",
])
def test_plain_text_is_a_search(text):
    assert classify(text) == "search"


@pytest.mark.parametrize("text", [
    "https://youtu.be/dQw4w9WgXcQ",
    "http://youtu.be/dQw4w9WgXcQ",       # plain http counts too
    "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    "https://youtube.com/watch?v=dQw4w9WgXcQ",
    "https://m.youtube.com/watch?v=dQw4w9WgXcQ",
    "https://music.youtube.com/watch?v=dQw4w9WgXcQ",
    "https://www.youtube.com/shorts/dQw4w9WgXcQ",
    "https://www.youtube.com/live/dQw4w9WgXcQ",
    "https://m.youtube.com/shorts/abc123",
    # a watch link that also carries a list queues the video, not the playlist
    f"https://www.youtube.com/watch?v=dQw4w9WgXcQ&list={PLAYLIST}",
    f"https://youtu.be/dQw4w9WgXcQ?list={PLAYLIST}",
])
def test_video_links(text):
    assert classify(text) == "video"


@pytest.mark.parametrize("text", [
    f"https://www.youtube.com/playlist?list={PLAYLIST}",
    f"https://youtube.com/playlist?list={PLAYLIST}",
    f"https://m.youtube.com/playlist?list={PLAYLIST}",
    "https://music.youtube.com/playlist?list=OLAK5uy_kSomething",
    f"https://www.youtube.com/watch?list={PLAYLIST}",      # no v=, so it's a playlist
    f"https://www.youtube.com/watch?index=3&list={PLAYLIST}",
])
def test_playlist_links(text):
    assert classify(text) == "playlist"


@pytest.mark.parametrize("text", [
    "https://vimeo.com/123456789",
    "https://soundcloud.com/artist/song",
    "https://example.com/watch?v=dQw4w9WgXcQ",   # a v= param isn't enough
    "https://notyoutube.com/watch?v=dQw4w9WgXcQ",
    "https://www.youtube.com/",
    "https://www.youtube.com/@somechannel",
    "https://www.youtube.com/c/somechannel",
])
def test_non_youtube_and_bare_paths_are_unsupported(text):
    assert classify(text) == "unsupported"


def test_scheme_check_is_case_sensitive():
    # urlparse copes with a capital scheme but classify's startswith does not,
    # so an upper-case link silently becomes a search. Recorded because it reads
    # as an oversight rather than a decision.
    assert classify("HTTPS://WWW.YOUTUBE.COM/watch?v=dQw4w9WgXcQ") == "search"


def test_bare_youtu_be_host_is_still_a_video():
    # youtu.be is accepted on the host alone, with no id behind it to play.
    assert classify("https://youtu.be/") == "video"


# ─────────────────────────────────── list_id ─────────────────────────────────
@pytest.mark.parametrize("text,expected", [
    (f"https://www.youtube.com/playlist?list={PLAYLIST}", PLAYLIST),
    (f"https://www.youtube.com/watch?v=dQw4w9WgXcQ&list={PLAYLIST}", PLAYLIST),
    (f"https://www.youtube.com/watch?list={PLAYLIST}&v=dQw4w9WgXcQ", PLAYLIST),
    ("https://music.youtube.com/playlist?list=OLAK5uy_kSomething", "OLAK5uy_kSomething"),
    (f"https://www.youtube.com/playlist?list={PLAYLIST}&list=PLsecond", PLAYLIST),
    ("https://youtu.be/dQw4w9WgXcQ", None),
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", None),
    ("https://www.youtube.com/shorts/abc123", None),
    ("https://www.youtube.com/playlist?list=", None),     # blank is dropped
    ("", None),
    ("never gonna give you up", None),
])
def test_list_id(text, expected):
    assert list_id(text) == expected


def test_every_playlist_carries_the_id_it_was_queued_from():
    url = f"https://www.youtube.com/playlist?list={PLAYLIST}"
    assert classify(url) == "playlist"
    assert list_id(url) == PLAYLIST


# ──────────────────────────────── fmt_duration ───────────────────────────────
@pytest.mark.parametrize("seconds,expected", [
    (None, "?:??"),
    (0, "?:??"),
    (5, "0:05"),
    (59, "0:59"),
    (60, "1:00"),
    (61, "1:01"),
    (599, "9:59"),
    (600, "10:00"),
    (3599, "59:59"),
    (3600, "1:00:00"),
    (3661, "1:01:01"),
    (86399, "23:59:59"),
    (90061, "25:01:01"),       # hours keep counting, they don't wrap
])
def test_fmt_duration(seconds, expected):
    assert fmt_duration(seconds) == expected


# ─────────────────────────────── Track.thumb ─────────────────────────────────
@pytest.mark.parametrize("url,video_id", [
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://youtube.com/watch?v=abc123", "abc123"),
    ("https://m.youtube.com/watch?v=abc123", "abc123"),
    ("https://music.youtube.com/watch?v=abc123", "abc123"),
    # the v= param is read first, so it wins over anything in the path
    (f"https://www.youtube.com/watch?v=fromquery&list={PLAYLIST}", "fromquery"),
    ("https://youtu.be/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://youtu.be/abc123?si=tracking_id", "abc123"),
    ("https://www.youtube.com/shorts/abc123", "abc123"),
    ("https://www.youtube.com/live/xyz789", "xyz789"),
])
def test_thumb_uses_the_video_id(url, video_id):
    assert track(url).thumb == f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg"


@pytest.mark.parametrize("url", [
    f"https://www.youtube.com/playlist?list={PLAYLIST}",    # a playlist has no picture
    "https://www.youtube.com/watch",                      # v= present but empty
    "https://www.youtube.com/",
    "",
])
def test_thumb_is_none_without_a_video_id(url):
    assert track(url).thumb is None
