"""
Mercedes me API client.

Uses the same mobile SDK backend as the official Mercedes me app
(PKCE OAuth2 flow, protobuf vehicle attributes).
"""

import base64
import hashlib
import json
import logging
import re
import secrets
import time
import urllib.parse
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

import asyncio

import aiohttp
from yarl import URL

log = logging.getLogger(__name__)

# ── Regional endpoints & app identity headers ─────────────────────────────────
_REGIONS = {
    "emea": {
        "auth": "https://id.mercedes-benz.com",
        "app_id": "62778dc4-1de3-44f4-af95-115f06a3a008",
        "bff": "https://bff.emea-prod.mobilesdk.mercedes-benz.com",
        "widget": "https://widget.emea-prod.mobilesdk.mercedes-benz.com",
        "websocket": "wss://websocket.emea-prod.mobilesdk.mercedes-benz.com/v2/ws",
        "app_name": "mycar-store-ece",
        "app_version": "1.68.0 (3060)",
        "sdk_version": "4.10.0",
        "user_agent": "Mercedes-Benz/3044 CFNetwork/3860.400.22 Darwin/25.3.0",
        "locale": "en-GB",
    },
    "noam": {
        "auth": "https://id.mercedes-benz.com",
        "app_id": "62778dc4-1de3-44f4-af95-115f06a3a008",
        "bff": "https://bff.amap-prod.mobilesdk.mercedes-benz.com",
        "widget": "https://widget.amap-prod.mobilesdk.mercedes-benz.com",
        "websocket": "wss://websocket.amap-prod.mobilesdk.mercedes-benz.com/v2/ws",
        "app_name": "mycar-store-us",
        "app_version": "3.67.0",
        "sdk_version": "4.10.0",
        "user_agent": "Mercedes-Benz/3044 CFNetwork/3860.400.22 Darwin/25.3.0",
        "locale": "en-US",
    },
    "china": {
        "auth": "https://ciam-1.mercedes-benz.com.cn",
        "app_id": "3f36efb1-f84b-4402-b5a2-68a118fec33e",
        "bff": "https://bff.cn-prod.mobilesdk.mercedes-benz.com",
        "widget": "https://widget.cn-prod.mobilesdk.mercedes-benz.com",
        "websocket": "wss://websocket.cn-prod.mobilesdk.mercedes-benz.com/v2/ws",
        "app_name": "mycar-store-cn",
        "app_version": "1.67.0",
        "sdk_version": "2.132.2",
        "user_agent": "MyStarCN/1.63.0 (com.daimler.ris.mercedesme.cn.ios; build:1758; iOS 16.3.1) Alamofire/5.4.0",
        "locale": "zh-CN",
    },
}

REDIRECT_URI = "rismycar://login-callback"
SCOPE = "email profile ciam-uid phone openid offline_access"
TOKEN_FILE = Path("/data/mercedes_token.json")
DEVICE_FILE = Path("/data/mercedes_device.json")
SAFARI_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 15_8_3 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/15.6.6 Mobile/15E148 Safari/604.1"
)
# Avoid hammering CIAM after failed logins (Mercedes returns HTTP 429).
AUTH_BACKOFF_SECONDS = 15 * 60


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    return verifier, challenge


def _load_or_create_device_id(path: Path = DEVICE_FILE) -> str:
    if path.exists():
        try:
            data = json.loads(path.read_text())
            device_id = data.get("device_id")
            if device_id:
                return device_id
        except Exception:
            pass
    device_id = str(uuid.uuid4())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"device_id": device_id}))
    return device_id


class MercedesClient:
    def __init__(
        self,
        email: str,
        password: str,
        vin: str = "",
        region: str = "emea",
        token_file: Path = TOKEN_FILE,
    ):
        self._email = email
        self._password = password
        self._vin = vin
        self._cfg = _REGIONS[region]
        self._token_file = token_file
        self._token: Optional[Dict] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._device_id = _load_or_create_device_id()
        self._auth_backoff_until = 0.0

    async def __aenter__(self):
        jar = aiohttp.CookieJar()
        jar.update_cookies(
            {"CIAM.DEVICE": self._device_id},
            response_url=URL(self._cfg["auth"]),
        )
        self._session = aiohttp.ClientSession(cookie_jar=jar)
        self._load_token()
        return self

    async def __aexit__(self, *args):
        if self._session:
            await self._session.close()

    # ── Token persistence ─────────────────────────────────────────────────────

    def _load_token(self) -> None:
        if self._token_file.exists():
            try:
                self._token = json.loads(self._token_file.read_text())
            except Exception:
                pass

    def _save_token(self) -> None:
        self._token_file.parent.mkdir(parents=True, exist_ok=True)
        self._token_file.write_text(json.dumps(self._token))

    def _store_token(self, token: Dict[str, Any]) -> None:
        """Persist token, keeping any existing refresh_token if omitted."""
        if "refresh_token" not in token and self._token and "refresh_token" in self._token:
            token["refresh_token"] = self._token["refresh_token"]
        token["expires_at"] = time.time() + token.get("expires_in", 3600)
        self._token = token
        self._save_token()
        self._auth_backoff_until = 0.0

    def _token_valid(self) -> bool:
        if not self._token:
            return False
        return time.time() < self._token.get("expires_at", 0) - 60

    def _ciam_headers(self, *, html: bool = False) -> Dict[str, str]:
        auth_base = self._cfg["auth"]
        if html:
            return {
                "user-agent": SAFARI_UA,
                "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "accept-language": "de-DE,de;q=0.9",
            }
        return {
            "accept": "application/json, text/plain, */*",
            "content-type": "application/json",
            "origin": auth_base,
            "referer": f"{auth_base}/ciam/auth/login",
            "accept-language": "de-DE,de;q=0.9",
            "user-agent": SAFARI_UA,
        }

    async def _refresh(self) -> bool:
        if not self._token or "refresh_token" not in self._token:
            return False
        try:
            async with self._session.post(
                f"{self._cfg['auth']}/as/token.oauth2",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": self._token["refresh_token"],
                    "client_id": self._cfg["app_id"],
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            ) as resp:
                if resp.status != 200:
                    return False
                t = await resp.json()
                self._store_token(t)
                return True
        except Exception as exc:
            log.warning("Token refresh failed: %s", exc)
            return False

    # ── Full PKCE auth flow ───────────────────────────────────────────────────

    async def authenticate(self) -> None:
        """OAuth2 PKCE login with username + password."""
        now = time.time()
        if now < self._auth_backoff_until:
            remaining = int(self._auth_backoff_until - now)
            raise RuntimeError(
                f"Mercedes auth in backoff after recent failure ({remaining}s left)"
            )

        verifier, challenge = _pkce()
        auth_base = self._cfg["auth"]
        app_id = self._cfg["app_id"]

        try:
            await self._authenticate_unlocked(verifier, challenge, auth_base, app_id)
        except Exception:
            self._auth_backoff_until = time.time() + AUTH_BACKOFF_SECONDS
            raise

    async def _authenticate_unlocked(
        self, verifier: str, challenge: str, auth_base: str, app_id: str
    ) -> None:
        # 1. GET authorization endpoint → follow redirects → resume is in final URL query string.
        # If an SSO session cookie is still active the server may skip the login form and redirect
        # directly to rismycar://login-callback?code=...; aiohttp raises InvalidURL for that scheme.
        final_url = ""
        try:
            async with self._session.get(
                f"{auth_base}/as/authorization.oauth2",
                params={
                    "client_id": app_id,
                    "response_type": "code",
                    "scope": SCOPE,
                    "redirect_uri": REDIRECT_URI,
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                },
                headers=self._ciam_headers(html=True),
                allow_redirects=True,
            ) as resp:
                final_url = str(resp.url)
        except Exception as exc:
            exc_str = str(exc)
            if "rismycar://" in exc_str:
                final_url = _extract_rismycar_url(exc_str)
                if not final_url:
                    raise
            else:
                raise

        # Fast-path: SSO session redirected straight to the callback URL with an auth code.
        if final_url.startswith(REDIRECT_URI):
            code_m = re.search(r"[?&]code=([^&]+)", final_url)
            if not code_m:
                raise RuntimeError(f"No auth code in SSO redirect: {final_url!r}")
            log.info("Mercedes SSO fast-path — exchanging code directly")
            await self._exchange_code(code_m.group(1), verifier, auth_base, app_id)
            return

        # resume=... lives in the query string of the redirected login page URL
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(final_url).query)
        resume_parts = qs.get("resume", [])
        if not resume_parts:
            raise RuntimeError(
                f"Could not find 'resume' param in Mercedes redirect URL: {final_url!r}"
            )
        resume = resume_parts[0]

        # 2. Register browser UA (required by the IdP)
        await self._session.post(
            f"{auth_base}/ciam/auth/ua",
            json={
                "browserName": "Mobile Safari",
                "browserVersion": "15.6.6",
                "osName": "iOS",
            },
            headers=self._ciam_headers(),
        )

        # 3. Submit username
        async with self._session.post(
            f"{auth_base}/ciam/auth/login/user",
            json={"username": self._email},
            headers=self._ciam_headers(),
        ) as resp:
            user_body = await resp.text()
            if resp.status == 429:
                raise RuntimeError(
                    "Mercedes CIAM rate-limited username login (HTTP 429). "
                    "Backing off before retry."
                )
            if resp.status >= 400:
                raise RuntimeError(f"Mercedes username login failed ({resp.status}): {user_body}")

        # 4. Submit password (rid is client-generated; Mercedes may then offer a passkey prompt)
        rid = secrets.token_urlsafe(24)
        async with self._session.post(
            f"{auth_base}/ciam/auth/login/pass",
            json={
                "username": self._email,
                "password": self._password,
                "rememberMe": False,
                "rid": rid,
            },
            headers=self._ciam_headers(),
        ) as resp:
            pass_body = await resp.text()
            if resp.status == 429:
                raise RuntimeError(
                    "Mercedes CIAM rate-limited password login (HTTP 429). "
                    "Backing off before retry."
                )
            if resp.status >= 400:
                raise RuntimeError(f"Mercedes password login failed ({resp.status}): {pass_body}")
            try:
                pass_resp = json.loads(pass_body)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"Mercedes password login returned non-JSON: {pass_body!r}") from exc

        # 4b. Decline passkey setup prompt when offered (new CIAM behaviour)
        if pass_resp.get("passkeyDemoEnabled"):
            log.info("Mercedes passkey prompt detected — declining to continue password login")
            async with self._session.post(
                f"{auth_base}/ciam/auth/disablePasskeyDemo",
                json={
                    "username": self._email,
                    "password": self._password,
                    "rememberMe": False,
                    "rid": rid,
                    "disablePasskeyDemo": True,
                },
                headers=self._ciam_headers(),
            ) as resp:
                skip_body = await resp.text()
                if resp.status >= 400:
                    raise RuntimeError(
                        f"Mercedes passkey prompt skip failed ({resp.status}): {skip_body}"
                    )
                try:
                    pass_resp = json.loads(skip_body)
                except json.JSONDecodeError as exc:
                    raise RuntimeError(
                        f"Mercedes passkey skip returned non-JSON: {skip_body!r}"
                    ) from exc

        result = pass_resp.get("result", "")
        pre_token = pass_resp.get("token", "")

        if result == "GOTO_LOGIN_OTP":
            raise RuntimeError(
                "Mercedes account has MFA enabled. "
                "Disable MFA (or create a separate account) to use carcharge."
            )
        if result not in ("RESUME2OIDCP", "GOTO_LOGIN_LEGAL_TEXTS"):
            raise RuntimeError(
                f"Unexpected Mercedes auth result: {result!r} (payload keys: {sorted(pass_resp)})"
            )

        # 5. Accept legal consent if prompted (first-time login)
        if result == "GOTO_LOGIN_LEGAL_TEXTS":
            async with self._session.post(
                f"{auth_base}/ciam/auth/toas/saveLoginConsent",
                json={
                    "texts": {},
                    "homeCountry": pass_resp.get("homeCountry") or "SE",
                    "consentCountry": pass_resp.get("consentCountry") or "SE",
                },
                headers=self._ciam_headers(),
            ) as resp:
                consent_body = await resp.text()
                if resp.status >= 400:
                    raise RuntimeError(
                        f"Mercedes legal consent failed ({resp.status}): {consent_body}"
                    )
                try:
                    consent_resp = json.loads(consent_body)
                except json.JSONDecodeError:
                    consent_resp = {}
                if consent_resp.get("result") == "RESUME2OIDCP" and consent_resp.get("token"):
                    pre_token = consent_resp["token"]
                elif not pre_token:
                    raise RuntimeError(
                        f"Mercedes legal consent did not return a resume token: {consent_resp}"
                    )

        if not pre_token:
            raise RuntimeError(f"Mercedes auth missing resume token after result {result!r}")

        # 6. Resume auth flow → POST form-encoded → redirects to rismycar://...?code=...
        resume_url = resume if resume.startswith("http") else f"{auth_base}{resume}"
        location = ""
        try:
            async with self._session.post(
                resume_url,
                data=aiohttp.FormData({"token": pre_token}),
                headers={
                    **self._ciam_headers(html=True),
                    "content-type": "application/x-www-form-urlencoded",
                    "origin": auth_base,
                    "referer": f"{auth_base}/ciam/auth/login",
                },
                allow_redirects=False,
            ) as resp:
                location = resp.headers.get("Location", "")
        except aiohttp.InvalidURL as exc:
            # aiohttp rejects the rismycar:// custom scheme — extract from the error string
            location = _extract_rismycar_url(str(exc))

        code_m = re.search(r"[?&]code=([^&]+)", location)
        if not code_m:
            raise RuntimeError(f"No auth code in redirect: {location!r}")

        # 7. Exchange code for tokens
        await self._exchange_code(code_m.group(1), verifier, auth_base, app_id)

    async def _exchange_code(
        self, code: str, verifier: str, auth_base: str, app_id: str
    ) -> None:
        async with self._session.post(
            f"{auth_base}/as/token.oauth2",
            data={
                "client_id": app_id,
                "code": code,
                "code_verifier": verifier,
                "grant_type": "authorization_code",
                "redirect_uri": REDIRECT_URI,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        ) as resp:
            t = await resp.json()

        if "access_token" not in t:
            raise RuntimeError(f"Token exchange failed: {t}")

        self._store_token(t)
        has_refresh = "refresh_token" in self._token
        log.info(
            "Mercedes authentication successful (refresh_token=%s)",
            "yes" if has_refresh else "no",
        )

    async def ensure_auth(self) -> None:
        if self._token_valid():
            return
        if not await self._refresh():
            await self.authenticate()

    # ── API calls ─────────────────────────────────────────────────────────────

    def _api_headers(self) -> Dict[str, str]:
        c = self._cfg
        return {
            "Authorization": f"Bearer {self._token['access_token']}",
            "X-SessionId": str(secrets.token_hex(16)).upper(),
            "X-TrackingId": str(secrets.token_hex(16)).upper(),
            "ris-os-name": "ios",
            "ris-os-version": "26.3",
            "X-ApplicationName": c["app_name"],
            "ris-application-version": c["app_version"],
            "ris-sdk-version": c["sdk_version"],
            "User-Agent": c["user_agent"],
            "X-Locale": c["locale"],
            "Content-Type": "application/json; charset=UTF-8",
        }

    async def get_vehicles(self) -> list:
        await self.ensure_auth()
        async with self._session.get(
            f"{self._cfg['bff']}/v2/vehicles",
            headers=self._api_headers(),
        ) as resp:
            data = await resp.json()
        if isinstance(data, list):
            return data
        return data.get("assignedVehicles", [])

    async def _resolve_vin(self) -> str:
        if self._vin:
            return self._vin
        vehicles = await self.get_vehicles()
        if not vehicles:
            raise RuntimeError("No vehicles found on Mercedes account")
        vin = vehicles[0].get("fin") or vehicles[0].get("vin") or ""
        log.info("Auto-detected VIN: %s", vin)
        self._vin = vin
        return vin

    async def _send_ws_command(self, cmd_bytes: bytes) -> None:
        """Send a serialized CommandRequest over the SDK WebSocket."""
        c = self._cfg
        headers = {
            "Authorization": f"Bearer {self._token['access_token']}",
            "X-SessionId": secrets.token_hex(16).upper(),
            "X-TrackingId": secrets.token_hex(16).upper(),
            "APP-SESSION-ID": secrets.token_hex(16).upper(),
            "OUTPUT-FORMAT": "PROTO",
            "ris-os-name": "ios",
            "ris-os-version": "26.3",
            "ris-sdk-version": c["sdk_version"],
            "X-Locale": c["locale"],
            "User-Agent": c["user_agent"],
            "X-ApplicationName": c["app_name"],
            "ris-application-version": c["app_version"],
        }
        try:
            async with self._session.ws_connect(c["websocket"], headers=headers) as ws:
                # Phase 1: ack initial server messages before sending command.
                # Server sends AppTwinPendingCommandsRequest (field 18) and
                # VEPUpdatesByVIN (field 2) on connect; both must be acked first.
                acked: set[int] = set()
                init_deadline = asyncio.get_event_loop().time() + 4.0
                while asyncio.get_event_loop().time() < init_deadline:
                    remaining = init_deadline - asyncio.get_event_loop().time()
                    try:
                        msg = await asyncio.wait_for(ws.receive(), timeout=min(remaining, 1.5))
                    except asyncio.TimeoutError:
                        break
                    if msg.type not in (aiohttp.WSMsgType.BINARY, aiohttp.WSMsgType.TEXT):
                        break
                    data = msg.data if isinstance(msg.data, bytes) else msg.data.encode()
                    if not data:
                        continue
                    outer_tag, tag_end = _decode_varint(data, 0)
                    outer_field = outer_tag >> 3
                    if outer_field == 18 and 18 not in acked:
                        # AppTwinPendingCommandsRequest → reply with field 21 (empty)
                        await ws.send_bytes(_make_client_msg(21, b""))
                        acked.add(18)
                        log.debug("WS: sent AppTwinPendingCommandsResponse")
                    elif outer_field == 2 and 2 not in acked:
                        # VEPUpdatesByVIN → reply with AcknowledgeVEPUpdatesByVIN (field 22)
                        inner_len, inner_start = _decode_varint(data, tag_end)
                        inner = data[inner_start: inner_start + inner_len]
                        seq = _extract_int_field(inner, 2) or 0
                        ack_content = b"\x08" + _encode_varint(seq)
                        await ws.send_bytes(_make_client_msg(22, ack_content))
                        acked.add(2)
                        log.debug("WS: sent AcknowledgeVEPUpdatesByVIN(seq=%d)", seq)
                    elif outer_field == 19 and 19 not in acked:
                        # AssignedVehicles → reply with AcknowledgeAssignedVehicles (field 23)
                        await ws.send_bytes(_make_client_msg(23, b""))
                        acked.add(19)
                        log.debug("WS: sent AcknowledgeAssignedVehicles")
                    if {18, 2}.issubset(acked):
                        break

                if not {18, 2}.issubset(acked):
                    log.warning("WS: init acks incomplete (acked=%s); sending command anyway", acked)

                # Phase 2: send the command
                await ws.send_bytes(_wrap_client_message(cmd_bytes))

                # Phase 3: wait for command status
                deadline = asyncio.get_event_loop().time() + 8.0
                while asyncio.get_event_loop().time() < deadline:
                    remaining = deadline - asyncio.get_event_loop().time()
                    try:
                        msg = await asyncio.wait_for(ws.receive(), timeout=remaining)
                    except asyncio.TimeoutError:
                        break
                    if msg.type not in (aiohttp.WSMsgType.BINARY, aiohttp.WSMsgType.TEXT):
                        break
                    data = msg.data if isinstance(msg.data, bytes) else msg.data.encode()
                    if not data:
                        continue
                    error = _extract_ris_error(data)
                    if error:
                        raise RuntimeError(f"Car rejected command: {error}")
        except aiohttp.WSServerHandshakeError as exc:
            log.error(
                "WS handshake failed (HTTP %s) — response headers: %s",
                exc.status,
                dict(exc.headers) if exc.headers else {},
            )
            raise

    async def _send_command(self, cmd_bytes: bytes, label: str) -> None:
        """Try WebSocket first, fall back to widget REST command endpoint."""
        try:
            await self._send_ws_command(cmd_bytes)
            return
        except Exception as ws_exc:
            log.warning("WS command failed (%s), trying REST fallback: %s", label, ws_exc)

        # REST fallback: POST raw CommandRequest to the widget command endpoint
        vin = await self._resolve_vin()
        url = f"{self._cfg['widget']}/v1/vehicle/{vin}/command"
        headers = {
            **self._api_headers(),
            "Content-Type": "application/x-protobuf",
            "Accept": "application/x-protobuf",
        }
        async with self._session.post(url, data=cmd_bytes, headers=headers) as resp:
            body = await resp.read()
            if resp.status != 200:
                log.error(
                    "REST command failed (HTTP %s) for %s: %s",
                    resp.status, label, body[:200],
                )
                raise RuntimeError(f"Command {label} failed via both WS and REST (HTTP {resp.status})")
            log.info("Command %s accepted via REST (HTTP 200)", label)

    async def send_precondition_now(self) -> None:
        """Send an immediate ZEV preconditioning start command."""
        await self.ensure_auth()
        vin = await self._resolve_vin()
        from carcharge.proto import vehicle_commands_pb2
        cmd = vehicle_commands_pb2.CommandRequest()
        cmd.vin = vin
        cmd.request_id = secrets.token_hex(8)
        cmd.zev_preconditioning_start.departure_time = 0
        cmd.zev_preconditioning_start.type = vehicle_commands_pb2.ZEVPreconditioningType.Value("NOW")
        await self._send_command(cmd.SerializeToString(), "ZEVPreconditioningStart")
        log.info("Precondition command sent for VIN %s", vin)

    async def send_charge_max_soc(self, soc_pct: int) -> None:
        """Set the DEFAULT charge program's max SoC target on the car."""
        await self.ensure_auth()
        vin = await self._resolve_vin()
        from carcharge.proto import vehicle_commands_pb2
        from google.protobuf.wrappers_pb2 import Int32Value
        cmd = vehicle_commands_pb2.CommandRequest()
        cmd.vin = vin
        cmd.request_id = secrets.token_hex(8)
        # DEFAULT_CHARGE_PROGRAM = 0; max_soc is an Int32Value wrapper
        cmd.charge_program_configure.charge_program = 0
        cmd.charge_program_configure.max_soc.CopyFrom(Int32Value(value=soc_pct))
        await self._send_command(cmd.SerializeToString(), f"ChargeProgramConfigure(max_soc={soc_pct}%)")
        log.info("Charge program max SoC set to %d%% for VIN %s", soc_pct, vin)

    async def get_vehicle_data(self) -> Dict[str, Any]:
        """
        Returns:
            soc (float|None): battery state of charge 0-100
            lat (float|None): latitude
            lon (float|None): longitude
            charging (bool|None): True if currently charging
        """
        await self.ensure_auth()
        vin = await self._resolve_vin()

        async with self._session.get(
            f"{self._cfg['widget']}/v1/vehicle/{vin}/vehicleattributes",
            headers={
                **self._api_headers(),
                "Accept": "application/x-protobuf",
            },
        ) as resp:
            if resp.status != 200:
                raise RuntimeError(f"vehicleattributes returned {resp.status}")
            raw = await resp.read()

        return _parse_vep_update(raw)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _encode_varint(n: int) -> bytes:
    result = bytearray()
    while n > 0x7F:
        result.append((n & 0x7F) | 0x80)
        n >>= 7
    result.append(n)
    return bytes(result)


def _decode_varint(data: bytes, pos: int) -> tuple[int, int]:
    """Parse a protobuf varint at data[pos], return (value, new_pos)."""
    result = 0
    shift = 0
    while pos < len(data):
        b = data[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        shift += 7
        if not (b & 0x80):
            break
    return result, pos


def _extract_int_field(data: bytes, field_number: int) -> Optional[int]:
    """Return the first varint value for field_number in a serialized protobuf message."""
    pos = 0
    while pos < len(data):
        field_tag, pos = _decode_varint(data, pos)
        wire_type = field_tag & 0x07
        fn = field_tag >> 3
        if wire_type == 0:
            val, pos = _decode_varint(data, pos)
            if fn == field_number:
                return val
        elif wire_type == 2:
            length, pos = _decode_varint(data, pos)
            pos += length
        elif wire_type == 1:
            pos += 8
        elif wire_type == 5:
            pos += 4
        else:
            break
    return None


def _make_client_msg(field_number: int, content: bytes) -> bytes:
    """Build a ClientMessage LEN field (wire type 2) for the given field_number."""
    tag = _encode_varint((field_number << 3) | 2)
    return tag + _encode_varint(len(content)) + content


def _wrap_client_message(command_request_bytes: bytes) -> bytes:
    """Wrap serialized CommandRequest in a ClientMessage (field 3, wire type 2)."""
    # tag = (field_number=3 << 3) | wire_type=2 = 0x1A
    return bytes([0x1A]) + _encode_varint(len(command_request_bytes)) + command_request_bytes


def _extract_rismycar_url(error_str: str) -> str:
    """Pull the rismycar:// URL out of an aiohttp InvalidURL error message."""
    m = re.search(r"rismycar://[^\s'\"]+", error_str)
    return m.group(0) if m else ""


def _extract_ris_error(data: bytes) -> Optional[str]:
    """Scan a raw WS response for a RIS error string embedded in the protobuf bytes."""
    # Error strings like "RIS_COULD_NOT_SEND_COMMAND" appear as UTF-8 in the binary payload
    try:
        text = data.decode("latin-1")
    except Exception:
        return None
    m = re.search(r"RIS_[A-Z_]+", text)
    return m.group(0) if m else None


def _parse_vep_update(data: bytes) -> Dict[str, Any]:
    """Parse VEPUpdate protobuf bytes into a simple dict."""
    try:
        from carcharge.proto import vehicle_events_pb2

        update = vehicle_events_pb2.VEPUpdate()
        update.ParseFromString(data)

        result: Dict[str, Any] = {"soc": None, "lat": None, "lon": None, "charging": None}
        for key, attr in update.attributes.items():
            kind = attr.WhichOneof("attribute_type")
            if kind is None:
                continue
            val = getattr(attr, kind)
            if key == "soc":
                result["soc"] = float(val)
            elif key == "positionLat":
                result["lat"] = float(val)
            elif key == "positionLong":
                result["lon"] = float(val)
            elif key == "chargingactive":
                result["charging"] = bool(val)
        return result
    except Exception as exc:
        log.error("Failed to parse VEPUpdate protobuf: %s", exc)
        return {"soc": None, "lat": None, "lon": None, "charging": None}
