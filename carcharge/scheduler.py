"""
Core service logic:
 - ChargingService: monitors car GPS, starts/schedules Epspot sessions
"""

import asyncio
import logging
import math
from datetime import datetime, time, timedelta
from typing import Optional

from .config import Config
from .epspot import EpspotClient
from .mercedes import MercedesClient
from .specialdays import SpecialDays
from .state import state
from .stats import ChargeStats
from .trips import cleanup_past_trips, save_trips

log = logging.getLogger(__name__)

_DAY = {0: "mon", 1: "tue", 2: "wed", 3: "thu", 4: "fri", 5: "sat", 6: "sun"}
_R = 6_371_000.0


def _distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    )
    return 2 * _R * math.asin(math.sqrt(a))


class ChargingService:
    GPS_TOLERANCE_M = 150

    def __init__(self, cfg: Config, epspot: EpspotClient, mercedes: MercedesClient):
        self.cfg = cfg
        self.epspot = epspot
        self.mercedes = mercedes
        self._last_plug_state: Optional[bool] = None
        self._last_sent_soc_target: Optional[int] = None
        self._trip_committed: Optional[datetime] = None  # departure we've started charging for
        self._committed_target: Optional[int] = None      # trip target held until unplug
        self._last_limit_source: Optional[str] = None
        self.special_days = SpecialDays()
        state.special_days = self.special_days
        self.stats = ChargeStats(is_special=self.special_days.is_special_dt)
        state.charge_stats = self.stats
        loc = cfg.charger_location
        self._charger_lat: Optional[float] = loc.lat if loc else None
        self._charger_lon: Optional[float] = loc.lon if loc else None

    # ── Scheduling helpers ────────────────────────────────────────────────────

    def _departure_still_scheduled(self, depart: datetime) -> bool:
        """True if this departure still exists in manual trips or the weekly schedule."""
        if any(t["depart_at"] == depart for t in state.trips):
            return True
        name = _DAY[depart.weekday()]
        for sched in self.cfg.charging.schedule:
            if name in sched.days:
                h, m = map(int, sched.depart_at.split(":"))
                if depart.time() == time(h, m):
                    return True
        return False

    def _next_departure(self) -> Optional[datetime]:
        # Keep a trip active (so the car holds its trip target and keeps charging)
        # right up until departure — NOT a few minutes before, or the target would
        # drop back to basic and stop the car just short of the target.
        now = datetime.now()
        candidates: list[datetime] = []

        # Weekly schedule: find the earliest upcoming slot
        for days_ahead in range(8):
            day = now.date() + timedelta(days=days_ahead)
            name = _DAY[day.weekday()]
            for sched in self.cfg.charging.schedule:
                if name in sched.days:
                    h, m = map(int, sched.depart_at.split(":"))
                    depart = datetime.combine(day, time(h, m))
                    if depart > now:
                        candidates.append(depart)
                        break  # one future schedule entry per day is enough
            if candidates:
                break  # stop scanning once we have the earliest weekly hit

        # One-off trips added via UI
        for trip in state.trips:
            depart = trip["depart_at"]
            if depart > now:
                candidates.append(depart)

        return min(candidates) if candidates else None

    def _trip_soc_for(self, departure: datetime) -> int:
        """Return the per-trip target SoC, falling back to the config default."""
        for trip in state.trips:
            if trip["depart_at"] == departure:
                return trip.get("soc_pct", self.cfg.charging.trip_soc_pct)
        return self.cfg.charging.trip_soc_pct

    def _start_time_for(self, current_soc: float, departure: datetime, target_soc: int) -> datetime:
        v = self.cfg.vehicle
        ch = self.cfg.charging
        # Step backward from the deadline so the start time accounts for both the
        # SoC taper and the time-of-day load profile the charge will pass through
        # (e.g. slow evening hours recovering to full rate overnight).
        deadline = departure - timedelta(minutes=ch.buffer_minutes)
        return self.stats.latest_start(
            current_soc, target_soc, deadline,
            capacity_kwh=v.battery_capacity_kwh,
            efficiency=v.charging_efficiency,
            fallback_rate_kw=v.charge_rate_kw,
        )

    # ── GPS ───────────────────────────────────────────────────────────────────

    def _car_is_here(self, lat: Optional[float], lon: Optional[float]) -> bool:
        if lat is None or lon is None or self._charger_lat is None or self._charger_lon is None:
            return False
        d = _distance_m(lat, lon, self._charger_lat, self._charger_lon)
        log.debug("Car is %.0f m from charger", d)
        return d <= self.GPS_TOLERANCE_M

    # ── Notifications ─────────────────────────────────────────────────────────

    async def _notify(self, message: str, title: str = "carcharge") -> None:
        if not self.cfg.ntfy_topic:
            return
        try:
            import aiohttp as _aio
            async with _aio.ClientSession() as s:
                await s.post(
                    f"https://ntfy.sh/{self.cfg.ntfy_topic}",
                    data=message.encode(),
                    headers={"Title": title},
                )
        except Exception as exc:
            log.warning("Notification failed: %s", exc)

    # ── Session management ────────────────────────────────────────────────────

    async def _start_smart_session(self, current_soc: Optional[float]) -> None:
        ch = self.cfg.charging
        v = self.cfg.vehicle

        if current_soc is None:
            log.warning("SoC unknown — assuming 0%% for safety")
            current_soc = 0.0

        now = datetime.now()
        departure = self._next_departure()
        state.next_departure = departure

        # Trip mode: next departure is within the lookahead window
        use_trip = (
            departure is not None
            and (departure - now).total_seconds() <= ch.trip_lookahead_hours * 3600
        )
        target = self._trip_soc_for(departure) if use_trip else ch.basic_soc_pct
        state.target_soc_pct = target

        # Push the target SoC to the car so it doesn't stop early at its own limit
        try:
            await self.mercedes.send_charge_max_soc(target)
        except Exception as exc:
            log.warning("Could not set car charge target to %d%%: %s", target, exc)

        if current_soc >= target:
            log.info("Battery %.0f%% ≥ target %d%% — nothing to do", current_soc, target)
            return

        energy_kwh = (
            (target - current_soc) / 100.0 * v.battery_capacity_kwh / v.charging_efficiency
        )
        delayed_start: Optional[datetime] = None

        if use_trip:
            t_start = self._start_time_for(current_soc, departure, target)
            state.next_charge_start = t_start
            if t_start > now + timedelta(minutes=2):
                delayed_start = t_start
                log.info(
                    "Trip mode: start at %s, %.1f kWh → %d%% by %s",
                    t_start.strftime("%H:%M"), energy_kwh, target,
                    departure.strftime("%a %H:%M"),
                )
            else:
                log.info("Trip mode: calculated start %s is past — starting immediately", t_start.strftime("%H:%M"))
        else:
            state.next_charge_start = now
            log.info("Basic mode: starting immediately → %d%% (%.1f kWh)", target, energy_kwh)

        session = await self.epspot.start_session(
            outlet_id=self.cfg.epspot.outlet_id,
            delayed_start_time=delayed_start,
            energy_limit_kwh=energy_kwh,
        )
        state.active_session_id = session.get("id")
        state.active_session_started = now

        mode_label = "trip" if use_trip else "basic"
        msg = (
            f"[{mode_label}] Charging scheduled at {delayed_start.strftime('%H:%M')} "
            f"(target {target}%, {energy_kwh:.1f} kWh)"
            if delayed_start
            else f"[{mode_label}] Charging started — target {target}%, {energy_kwh:.1f} kWh"
        )
        if departure and delayed_start:
            msg += f", ready by {departure.strftime('%a %H:%M')}"
        log.info(msg)
        await self._notify(msg)

        if self.cfg.climate_prep_enabled and use_trip and departure:
            prep_at = departure - timedelta(minutes=self.cfg.climate_prep_minutes_before)
            state.climate_prep_at = prep_at
            if prep_at > datetime.now():
                asyncio.ensure_future(self._schedule_climate_prep(prep_at))

    # ── Unplug handler ───────────────────────────────────────────────────────

    async def _handle_unplug(self) -> None:
        """Cable removed. Restore car charge target to basic SoC."""
        ch = self.cfg.charging
        if self._last_sent_soc_target not in (None, ch.basic_soc_pct):
            log.info("Plug removed — resetting car charge target to basic %d%%", ch.basic_soc_pct)
            try:
                await self.mercedes.send_charge_max_soc(ch.basic_soc_pct)
            except Exception as exc:
                log.warning("Could not reset car charge target after unplug: %s", exc)
        self._last_sent_soc_target = None
        self._trip_committed = None
        self._committed_target = None

    # ── Climate prep ──────────────────────────────────────────────────────────

    async def _schedule_climate_prep(self, prep_at: datetime) -> None:
        delay = (prep_at - datetime.now()).total_seconds()
        if delay > 0:
            log.info("Climate prep scheduled at %s (in %.0f min)", prep_at.strftime("%H:%M"), delay / 60)
            await asyncio.sleep(delay)
        log.info("Sending climate preconditioning command")
        try:
            await self.mercedes.send_precondition_now()
            await self._notify("Climate preconditioning started", title="carcharge")
        except Exception as exc:
            log.error("Climate prep failed: %s", exc)
            state.last_error = str(exc)

    # ── Plug-in handler ───────────────────────────────────────────────────────

    async def _handle_plug_in(self) -> None:
        """Cable just inserted. GPS-confirm it's our EQB, then start a session."""
        timeout = self.cfg.autostart.gps_confirm_timeout_seconds
        deadline = datetime.now() + timedelta(seconds=timeout)
        log.info("Plug inserted — confirming it's our EQB via GPS (up to %ds)", timeout)

        while datetime.now() < deadline:
            # Use freshly-fetched GPS from the regular Mercedes refresh
            # Check cached GPS first; fall back to a fresh API call
            gps_ok = self._car_is_here(state.mercedes_lat, state.mercedes_lon)
            confirmed_soc: Optional[float] = state.mercedes_soc if gps_ok else None
            if not gps_ok:
                try:
                    vdata = await self.mercedes.get_vehicle_data()
                    _update_mercedes_state(vdata)
                    if self._car_is_here(vdata.get("lat"), vdata.get("lon")):
                        gps_ok = True
                        confirmed_soc = vdata.get("soc")
                except Exception as exc:
                    log.warning("GPS/vehicle error during confirmation: %s", exc)

            if gps_ok:
                if state.active_session_id:
                    log.info("Plug-in confirmed but session %s already active", state.active_session_id)
                    return
                log.info("GPS confirmed: EQB at charger (soc=%.0f%%)", confirmed_soc or 0)
                await self._start_smart_session(confirmed_soc)
                return

            await asyncio.sleep(60)

        log.warning("GPS timed out — unknown vehicle plugged in, NOT starting.")
        await self._notify("Unknown vehicle plugged in — charging NOT started.", title="carcharge: unknown car")

    # ── Main loop ─────────────────────────────────────────────────────────────

    async def _refresh_epspot_meta(self) -> None:
        """Refresh balance and session info (called once per minute)."""
        try:
            user = await self.epspot.get_user_me()
            acct = user.get("epspotAccount_SEK") or {}
            bal = acct.get("balance")
            if bal is not None:
                state.epspot_balance_sek = float(bal)
            sessions = await self.epspot.get_active_sessions()
            if sessions:
                s = sessions[0]
                state.active_session_id = s.get("id")
                kwh = s.get("kWh") or (s.get("energyWh", 0) / 1000)
                state.active_session_kwh = kwh or None
            # Don't clear active_session_id on empty — outlet hardware controls that
        except Exception as exc:
            log.warning("Epspot meta refresh failed: %s", exc)

    async def _refresh_mercedes(self) -> None:
        try:
            vdata = await self.mercedes.get_vehicle_data()
            _update_mercedes_state(vdata)
            state.mercedes_ok = True
        except Exception as exc:
            log.warning("Mercedes refresh failed: %s", exc)
            state.mercedes_ok = False
            state.last_error = str(exc)

    async def run(self) -> None:
        # Fetch charger GPS from Epspot; overrides config if available
        if self._charger_lat is not None:
            log.info("Charger location from config: %.5f, %.5f", self._charger_lat, self._charger_lon)
        else:
            try:
                loc = await self.epspot.get_outlet_location(self.cfg.epspot.outlet_id)
                if loc:
                    self._charger_lat, self._charger_lon = loc
                    log.info("Charger location from Epspot: %.5f, %.5f", self._charger_lat, self._charger_lon)
                else:
                    log.warning("No charger location in config or Epspot — GPS checks disabled")
            except Exception as exc:
                log.warning("Could not fetch charger location from Epspot: %s", exc)
        state.charger_lat = self._charger_lat
        state.charger_lon = self._charger_lon

        # Populate active session state before the first outlet poll
        await self._refresh_epspot_meta()
        state.last_epspot_poll = datetime.now()

        interval = self.cfg.autostart.poll_interval_seconds
        log.info(
            "Service running (autostart=%s, poll=%ds)",
            self.cfg.autostart.enabled, interval,
        )

        tick = 0
        while True:
            # ── Every tick: Epspot outlet status + plug detection ─────────
            try:
                outlet = await self.epspot.get_outlet_status(self.cfg.epspot.outlet_id)
                state.outlet_plug_inserted = outlet["plugInserted"]
                state.outlet_status = outlet["status"]
                state.outlet_power_w = outlet.get("power_w", 0)
                state.outlet_amps = outlet.get("amps")
                state.outlet_capped_amps = outlet.get("capped_amps")
                state.outlet_max_amps = outlet.get("max_amps")
                state.outlet_evse_max_amps = outlet.get("evse_max_amps")
                limit_source = outlet.get("limit_source", "idle")
                if limit_source != self._last_limit_source:
                    if limit_source in ("system", "car"):
                        log.info(
                            "Charge limited by %s: drawing %.1f A of %s A offered (rating %s A)",
                            limit_source, (state.outlet_amps or 0),
                            state.outlet_evse_max_amps, state.outlet_max_amps,
                        )
                    self._last_limit_source = limit_source
                state.outlet_limit_source = limit_source

                # Learn the real charging power (tagged with SoC + time context,
                # plus the system-allowed ceiling so car vs system limits separate)
                self.stats.record(
                    state.outlet_power_w, state.mercedes_soc,
                    amps=outlet.get("amps"),
                    capped_amps=outlet.get("capped_amps"),
                    max_amps=outlet.get("max_amps"),
                    evse_max_amps=outlet.get("evse_max_amps"),
                )
                state.learned_charge_rate_kw = self.stats.effective_rate_kw()
                state.charge_samples = self.stats.sample_count
                # Outlet hardware is authoritative for session presence
                if outlet.get("session_id"):
                    state.active_session_id = outlet["session_id"]
                elif not outlet.get("plugInserted"):
                    state.active_session_id = None
                    state.active_session_kwh = None

                plug_in = bool(outlet["plugInserted"])
                just_plugged = plug_in and not self._last_plug_state
                just_unplugged = not plug_in and bool(self._last_plug_state)
                if self.cfg.autostart.enabled and just_plugged:
                    asyncio.ensure_future(self._handle_plug_in())
                if just_unplugged:
                    asyncio.ensure_future(self._handle_unplug())
                self._last_plug_state = plug_in
            except Exception as exc:
                log.warning("Epspot outlet poll failed: %s", exc)

            # ── Every ~60s: Epspot balance + active sessions ──────────────
            if tick % max(1, 60 // interval) == 0:
                await self._refresh_epspot_meta()
                state.last_epspot_poll = datetime.now()

            # ── Every tick: Mercedes GPS ───────────────────────────────────
            await self._refresh_mercedes()

            # ── Departure / target updates (just-in-time) ─────────────────
            # Recompute every tick from the CURRENT SoC and learned charge rate:
            # hold at basic until it's actually time to charge for the trip, then
            # raise the target. Holding basic while already above it pauses the car,
            # so a trip target set far too early doesn't charge to 100% for hours.
            # Once committed, the trip target is held until the car UNPLUGS — never
            # dropped at departure — so pre-conditioning draws keep topping back up
            # to the trip target until the user has actually left.
            cleanup_past_trips()
            now = datetime.now()
            dep = self._next_departure()
            state.next_departure = dep
            ch = self.cfg.charging
            soc = state.mercedes_soc

            # Drop a commitment only if the trip was deleted before it departed
            # (a future departure that no longer exists); a departed trip stays
            # committed until unplug. Unplug also clears it (_handle_unplug).
            if (self._trip_committed is not None and self._trip_committed > now
                    and not self._departure_still_scheduled(self._trip_committed)):
                self._trip_committed = None
                self._committed_target = None

            effective_target = ch.basic_soc_pct
            start_time: Optional[datetime] = None
            paused_for_trip = False
            trip_pending = False
            if self._trip_committed is not None:
                # Committed: hold the trip target until unplug (keeps topping up
                # any pre-conditioning draw; survives past departure).
                effective_target = self._committed_target
            elif dep is not None:
                trip_target = self._trip_soc_for(dep)
                within_lookahead = (dep - now).total_seconds() <= ch.trip_lookahead_hours * 3600
                if within_lookahead:
                    # latest_start returns the deadline itself when already at target,
                    # so this also commits ~buffer before departure to hold/top up.
                    # Unknown SoC → assume 0% so a trip still starts instead of
                    # silently holding basic until Mercedes data arrives.
                    if soc is None:
                        log.warning("SoC unknown — assuming 0%% to schedule trip charge")
                        soc = 0.0
                    start_time = self._start_time_for(soc, dep, trip_target)
                    if now >= start_time:
                        effective_target = trip_target
                        self._trip_committed = dep      # hold until unplug
                        self._committed_target = trip_target
                    else:
                        paused_for_trip = True  # hold basic until start_time
                        trip_pending = True
            state.next_charge_start = start_time
            state.target_soc_pct = effective_target
            state.trip_pending = trip_pending
            state.trip_pending_target = self._trip_soc_for(dep) if trip_pending and dep else None

            # Keep the car's configured max-SoC equal to our intended target. Lowering
            # it below the current SoC stops the car immediately (a pause); raising it
            # resumes. Always push on change — never skip just because SoC already
            # exceeds the target, or we could never pause.
            if effective_target != self._last_sent_soc_target and state.outlet_plug_inserted:
                try:
                    await self.mercedes.send_charge_max_soc(effective_target)
                    self._last_sent_soc_target = effective_target
                    if paused_for_trip:
                        log.info(
                            "Pausing trip charge: SoC %.0f%% holds at basic %d%% until %s "
                            "(target %d%% by %s)",
                            soc, ch.basic_soc_pct, start_time.strftime("%a %H:%M"),
                            trip_target, dep.strftime("%a %H:%M"),
                        )
                    else:
                        log.info("Car charge target set to %d%%", effective_target)
                except Exception as exc:
                    log.warning("Could not update car charge target to %d%%: %s", effective_target, exc)

            tick += 1
            await asyncio.sleep(interval)


def _update_mercedes_state(vdata: dict) -> None:
    state.mercedes_soc = vdata.get("soc")
    state.mercedes_lat = vdata.get("lat")
    state.mercedes_lon = vdata.get("lon")
    state.mercedes_charging = vdata.get("charging")
    state.mercedes_updated_at = datetime.now()
