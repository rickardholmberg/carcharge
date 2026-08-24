"""Shared live state, written by the service loop and read by the web UI."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional


@dataclass
class AppState:
    # ── Epspot ────────────────────────────────────────────────────────────────
    epspot_ok: bool = False
    epspot_user: str = ""
    epspot_balance_sek: Optional[float] = None
    outlet_plug_inserted: Optional[bool] = None
    outlet_status: str = ""
    outlet_power_w: int = 0
    outlet_amps: Optional[float] = None
    outlet_capped_amps: Optional[int] = None  # static outlet rating
    outlet_max_amps: Optional[int] = None     # hardware/cable max
    outlet_evse_max_amps: Optional[int] = None  # live EVSE-offered current (system limit)
    outlet_limit_source: str = "idle"          # "car" | "system" | "full" | "idle"
    active_session_id: Optional[str] = None
    active_session_started: Optional[datetime] = None
    active_session_kwh: Optional[float] = None

    # ── Mercedes ──────────────────────────────────────────────────────────────
    mercedes_ok: bool = False
    mercedes_soc: Optional[float] = None          # 0‑100
    mercedes_lat: Optional[float] = None
    mercedes_lon: Optional[float] = None
    mercedes_charging: Optional[bool] = None
    mercedes_updated_at: Optional[datetime] = None

    # ── Charger location (fetched from Epspot or config) ──────────────────────
    charger_lat: Optional[float] = None
    charger_lon: Optional[float] = None

    # ── Scheduler ─────────────────────────────────────────────────────────────
    next_departure: Optional[datetime] = None
    next_charge_start: Optional[datetime] = None
    trip_pending: bool = False           # trip scheduled, holding basic until start_time
    trip_pending_target: Optional[int] = None
    climate_prep_at: Optional[datetime] = None
    last_epspot_poll: Optional[datetime] = None
    last_error: Optional[str] = None

    # ── Config snapshot (set once at startup) ─────────────────────────────────
    basic_soc_pct: int = 80
    trip_soc_pct: int = 90
    target_soc_pct: int = 80          # effective target; updated dynamically
    charge_rate_kw: float = 11.0
    battery_capacity_kwh: float = 66.5
    charging_efficiency: float = 0.90

    # ── Learned charging-power statistics ─────────────────────────────────────
    learned_charge_rate_kw: Optional[float] = None  # global mean, None until enough data
    charge_samples: int = 0
    charge_stats: Optional[object] = None  # ChargeStats instance, for the web estimate
    special_days: Optional[object] = None  # SpecialDays instance, for the web API

    # ── Manually added one-off trips ───────────────────────────────────────────
    trips: List[dict] = field(default_factory=list)  # [{"id": str, "depart_at": datetime}]


# Single global instance shared across all coroutines
state = AppState()
