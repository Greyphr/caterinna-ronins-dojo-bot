import logging
import time
from typing import Dict, List, Optional

import aiohttp

logger = logging.getLogger("twitch_api")


class TwitchRateLimited(Exception):
    """Raised when Twitch rate-limits the app. Callers should skip the
    current check cycle rather than crash."""

    def __init__(self, reset_at: Optional[float] = None):
        self.reset_at = reset_at
        super().__init__("Twitch is rate-limiting this app")


class TwitchAPI:
    """Thin wrapper around the Twitch Helix API using an app access token
    (client credentials flow). No login/OAuth is needed from you or your
    friends — this only reads public stream data."""

    def __init__(self, client_id: str, client_secret: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self.access_token: Optional[str] = None
        self.token_expiry: float = 0

    def _clear_token(self) -> None:
        self.access_token = None
        self.token_expiry = 0

    async def _get_token(self, session: aiohttp.ClientSession, force: bool = False) -> str:
        if (
            not force
            and self.access_token
            and time.time() < self.token_expiry - 60
        ):
            return self.access_token

        url = "https://id.twitch.tv/oauth2/token"
        params = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "client_credentials",
        }
        async with session.post(url, params=params) as resp:
            if resp.status == 429:
                logger.warning(
                    "Twitch token endpoint rate-limited (429), reset at %s.",
                    _ratelimit_reset(resp),
                )
                raise TwitchRateLimited(_ratelimit_reset(resp))
            resp.raise_for_status()
            data = await resp.json()
            self.access_token = data["access_token"]
            self.token_expiry = time.time() + data["expires_in"]
            return self.access_token

    async def _get_json(
        self,
        session: aiohttp.ClientSession,
        url: str,
        params,
        retry_on_401: bool = True,
    ) -> dict:
        token = await self._get_token(session)
        headers = {
            "Client-ID": self.client_id,
            "Authorization": f"Bearer {token}",
        }
        async with session.get(url, headers=headers, params=params) as resp:
            if resp.status == 429:
                reset = _ratelimit_reset(resp)
                logger.warning("Twitch rate-limited (429), reset at %s.", reset)
                raise TwitchRateLimited(reset)
            if resp.status == 401 and retry_on_401:
                logger.info("Twitch returned 401; refreshing the token and retrying once.")
                self._clear_token()
                # The recursive call fetches a fresh token itself.
                return await self._get_json(session, url, params, retry_on_401=False)
            resp.raise_for_status()
            return await resp.json()

    async def get_live_streams(self, usernames: List[str]) -> Dict[str, dict]:
        """Returns dict of lowercase username -> stream data for whoever
        in the list is currently live."""
        if not usernames:
            return {}

        live: Dict[str, dict] = {}
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            url = "https://api.twitch.tv/helix/streams"
            # Helix allows up to 100 user_login params per request, so send
            # bigger lists (many servers combined) in chunks of 100.
            for i in range(0, len(usernames), 100):
                params = [("user_login", name) for name in usernames[i : i + 100]]
                data = await self._get_json(session, url, params)
                for s in data.get("data", []):
                    live[s["user_login"].lower()] = s
        return live

    async def get_user_info(self, username: str) -> Optional[dict]:
        """Used to validate a username exists when someone runs /addstreamer."""
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            url = "https://api.twitch.tv/helix/users"
            data = await self._get_json(session, url, {"login": username})
            results = data.get("data", [])
            return results[0] if results else None


def _ratelimit_reset(resp: aiohttp.ClientResponse) -> Optional[float]:
    value = resp.headers.get("Ratelimit-Reset")
    if value:
        try:
            return float(value)
        except ValueError:
            pass
    return None