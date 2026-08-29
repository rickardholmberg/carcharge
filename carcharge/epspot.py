import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

import aiohttp

log = logging.getLogger(__name__)

BASE_URL = "https://infrastructure.epspot.com/"
VERSION = "0.1.5"


def _limit_source(amps: Optional[float], evse_max_amps: Optional[int],
                  max_amps: Optional[int]) -> str:
    """Who is currently limiting the charge — the same distinction the Epspot app
    shows. Derived from the car's draw vs the EVSE's offered current vs the rating.
      • "idle"   — not charging / no offer
      • "car"    — drawing less than offered (car/taper is the constraint)
      • "system" — taking the full offer, but the offer is throttled below the rating
      • "full"   — taking the full offer at the outlet's full rating
    """
    if amps is None or not evse_max_amps:
        return "idle"
    if amps < evse_max_amps - 1:
        return "car"
    if max_amps and evse_max_amps < max_amps - 1:
        return "system"
    return "full"


class EpspotClient:
    def __init__(self, email: str, password: str):
        self._email = email
        self._password = password
        self._access_token: Optional[str] = None
        self._user_name: Optional[str] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._outlet_refs: Dict[str, Dict] = {}  # cached per-outlet config

    async def __aenter__(self):
        self._session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, *args):
        if self._session:
            await self._session.close()

    def _headers(self, auth: bool = True) -> Dict[str, str]:
        h: Dict[str, str] = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "com-epspot-version": VERSION,
        }
        if auth and self._access_token:
            h["Authorization"] = f"Bearer {self._access_token}"
        return h

    async def login(self) -> None:
        url = BASE_URL + "infrastructure/user/login"
        body = {"userName": self._email.lower(), "password": self._password}
        async with self._session.post(
            url, json=body, headers=self._headers(auth=False)
        ) as resp:
            data = await resp.json()
        if resp.status != 200:
            raise RuntimeError(f"Epspot login failed ({resp.status}): {data}")
        self._access_token = data.get("accessToken") or data.get("token")
        self._user_name = data.get("userName") or self._email.lower()
        log.info("Logged into Epspot as %s", self._user_name)

    async def _fetch_json(self, url: str) -> Dict[str, Any]:
        async with self._session.get(url, headers=self._headers()) as resp:
            if resp.status == 401:
                log.info("Token expired, re-logging in")
                await self.login()
                async with self._session.get(url, headers=self._headers()) as resp2:
                    return await resp2.json()
            return await resp.json()

    async def get_outlet_status(self, outlet_id: str) -> Dict[str, Any]:
        """
        Returns live outlet status: {plugInserted, status, power_w, active_energy_wh}.
        Automatically uses the correct endpoint (new vs legacy model).
        """
        if outlet_id not in self._outlet_refs:
            cfg = await self._fetch_json(BASE_URL + f"infrastructure/outletv2/{outlet_id}")
            self._outlet_refs[outlet_id] = {
                "use_new_model": cfg.get("useNewModel", False),
                "plant_ref": cfg.get("plantRef", ""),
                "provider_ref": cfg.get("providerRef", ""),
                "lat": cfg.get("locationLat"),
                "lon": cfg.get("locationLon"),
            }

        refs = self._outlet_refs[outlet_id]
        if refs["use_new_model"]:
            url = (BASE_URL
                   + f"infrastructure/provider/{refs['provider_ref']}"
                   + f"/plant/{refs['plant_ref']}"
                   + f"/outlet/{outlet_id}/status")
        else:
            url = BASE_URL + f"infrastructure/outlet/{outlet_id}/status"

        data = await self._fetch_json(url)

        # Normalise into a flat dict so callers don't need to know the shape
        module = data.get("moduleRequest", {})
        info = module.get("info", {})
        session_info = info.get("session", {})
        session_id = session_info.get("sessionId")
        # Capacity fields (amps): max is the hardware/cable ceiling; capped is what
        # the shared-supply load balancer currently allows this outlet. capped <
        # max means the *system* is limiting; the car limits when it draws < capped.
        max_amps = module.get("_outletMaxCapacity")
        capped_amps = module.get("_outletCappedCapacity")
        # evseMaxAmps is the *live* current the EVSE offers the car — the dynamic
        # load-balancer limit (drops in the evening as neighbours load the group).
        # _outletCappedCapacity is only the static outlet rating, so it never moves.
        evse_max_amps = module.get("_response", {}).get("evseMaxAmps")
        # info.amps is in milliamps while the capacity fields are whole amps;
        # normalise to amps (a real AC charge is < 100 A, mA readings are > 100).
        raw_amps = info.get("amps")
        amps = (raw_amps / 1000.0) if (raw_amps and raw_amps > 100) else raw_amps
        # Epspot reports instantaneous power in milliwatts; normalise to watts.
        return {
            "plugInserted": data.get("plugInserted", False),
            "status": data.get("status", ""),
            "power_w": round(info.get("power", 0) / 1000),
            "amps": amps,
            "max_amps": max_amps,
            "capped_amps": capped_amps,
            "evse_max_amps": evse_max_amps,
            "limit_source": _limit_source(amps, evse_max_amps, max_amps),
            "active_energy_wh": session_info.get("activeEnergy", 0) / 1000,
            "session_id": str(session_id) if session_id else None,
        }

    async def get_outlet_location(self, outlet_id: str) -> Optional[tuple]:
        """Return (lat, lon) for the outlet's registered location, or None."""
        if outlet_id not in self._outlet_refs:
            await self.get_outlet_status(outlet_id)  # populates cache
        refs = self._outlet_refs.get(outlet_id, {})
        lat, lon = refs.get("lat"), refs.get("lon")
        if lat is not None and lon is not None:
            return (lat, lon)
        return None

    async def get_user_me(self) -> Dict[str, Any]:
        """Returns user profile including epspotAccount_SEK.balance."""
        url = BASE_URL + "infrastructure/user/me/"
        async with self._session.get(url, headers=self._headers()) as resp:
            return await resp.json()

    async def get_active_sessions(self) -> List[Dict[str, Any]]:
        url = (
            BASE_URL
            + f"infrastructure/user/{self._user_name}/sessions?filter=status EQ ACTIVE"
        )
        async with self._session.get(url, headers=self._headers()) as resp:
            data = await resp.json()
        return data.get("items", [])

    async def start_session(
        self,
        outlet_id: str,
        delayed_start_time: Optional[datetime] = None,
        energy_limit_kwh: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        Start (or schedule) a charging session.

        delayed_start_time: if set, the charger waits until this moment to begin.
        energy_limit_kwh: if set, the charger auto-stops after delivering this energy.
        """
        url = BASE_URL + "infrastructure/session/start"
        body: Dict[str, Any] = {
            "outletId": outlet_id,
        }
        if energy_limit_kwh is not None:
            body["limitType"] = "ENERGY"
            # API uses kWh * 1e6 units (as observed in the app source)
            body["limit"] = round(energy_limit_kwh * 1_000_000)
        else:
            body["limitType"] = "NONE"
        if delayed_start_time is not None:
            # dayjs .format() produces ISO 8601
            body["delayedStartTime"] = delayed_start_time.isoformat()

        async with self._session.post(url, json=body, headers=self._headers()) as resp:
            data = await resp.json()
        if resp.status != 200:
            raise RuntimeError(f"Start session failed ({resp.status}): {data}")
        log.info("Session created: id=%s", data.get("id"))
        return data

    async def stop_session(self, session_id: str) -> Dict[str, Any]:
        url = BASE_URL + f"infrastructure/user/session/{session_id}/stop"
        async with self._session.post(url, headers=self._headers()) as resp:
            try:
                data = await resp.json()
            except Exception:
                data = {"raw": await resp.text()}
        if resp.status not in (200, 204):
            raise RuntimeError(f"Stop session failed ({resp.status}): {data}")
        log.info("Session stopped: id=%s (%s)", session_id, data)
        return data
