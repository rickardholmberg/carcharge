from dataclasses import dataclass, field
from typing import List, Optional
import yaml


@dataclass
class EpspotConfig:
    email: str
    password: str
    outlet_id: str


@dataclass
class MercedesConfig:
    email: str
    password: str
    vin: str = ""
    region: str = "emea"


@dataclass
class VehicleConfig:
    battery_capacity_kwh: float = 66.5
    charge_rate_kw: float = 11.0
    charging_efficiency: float = 0.90


@dataclass
class DepartureSchedule:
    days: List[str]
    depart_at: str  # "HH:MM"


@dataclass
class ChargingConfig:
    basic_soc_pct: int = 80
    trip_soc_pct: int = 90
    trip_lookahead_hours: float = 36.0
    buffer_minutes: int = 20
    schedule: List[DepartureSchedule] = field(default_factory=list)


@dataclass
class AutostartConfig:
    enabled: bool = True
    poll_interval_seconds: int = 30
    gps_confirm_timeout_seconds: int = 300


@dataclass
class LocationConfig:
    lat: float
    lon: float


@dataclass
class Config:
    epspot: EpspotConfig
    mercedes: MercedesConfig
    vehicle: VehicleConfig
    charging: ChargingConfig
    autostart: AutostartConfig
    charger_location: Optional[LocationConfig] = None
    ntfy_topic: str = ""
    climate_prep_enabled: bool = False
    climate_prep_minutes_before: int = 15


def load_config(path: str) -> Config:
    with open(path) as f:
        data = yaml.safe_load(f)

    e = data["epspot"]
    m = data["mercedes"]
    v = data.get("vehicle", {})
    ch = data.get("charging", {})
    a = data.get("autostart", {})
    loc = data.get("charger_location")

    schedules = [
        DepartureSchedule(days=s["days"], depart_at=s["depart_at"])
        for s in ch.get("schedule", [])
    ]

    return Config(
        epspot=EpspotConfig(
            email=e["email"],
            password=e["password"],
            outlet_id=e["outlet_id"],
        ),
        mercedes=MercedesConfig(
            email=m["email"],
            password=m["password"],
            vin=m.get("vin", ""),
            region=m.get("region", "emea"),
        ),
        vehicle=VehicleConfig(
            battery_capacity_kwh=v.get("battery_capacity_kwh", 66.5),
            charge_rate_kw=v.get("charge_rate_kw", 11.0),
            charging_efficiency=v.get("charging_efficiency", 0.90),
        ),
        charging=ChargingConfig(
            basic_soc_pct=ch.get("basic_soc_pct", ch.get("target_soc_pct", 80)),
            trip_soc_pct=ch.get("trip_soc_pct", 90),
            trip_lookahead_hours=ch.get("trip_lookahead_hours", 36.0),
            buffer_minutes=ch.get("buffer_minutes", 20),
            schedule=schedules,
        ),
        autostart=AutostartConfig(
            enabled=a.get("enabled", True),
            poll_interval_seconds=a.get("poll_interval_seconds", 30),
            gps_confirm_timeout_seconds=a.get("gps_confirm_timeout_seconds", 300),
        ),
        charger_location=LocationConfig(lat=loc["lat"], lon=loc["lon"]) if loc else None,
        ntfy_topic=data.get("notifications", {}).get("ntfy_topic", ""),
        climate_prep_enabled=data.get("climate_prep", {}).get("enabled", False),
        climate_prep_minutes_before=data.get("climate_prep", {}).get("minutes_before", 15),
    )
