"""
Charging-power statistics with a lightweight Bayesian model.

The delivered AC power is not constant. It depends on:
  • State of charge — power tapers as the battery fills (physical, fast to learn).
  • Time of day / weekday — this charger shares a supply with neighbours and is
    load-limited: it can sit at ~1.8 kW in the evening and recover to ~6.8 kW
    later as other cars finish. A strong, real, time-structured effect.

We log every sample with its full context (hour, weekend, SoC) and estimate the
rate with **Bayesian shrinkage**: each bin's posterior mean is a pseudo-count-
weighted average of the bin's own samples and the global mean, so a sparse bin
falls back toward the global rate while a well-sampled bin trusts itself. SoC and
time-of-day are treated as separable multiplicative effects (base rate × load
factor) to avoid the sparsity of a full cross-product of bins.

Because a single charge can span the evening→night load transition, duration is
estimated by a **time-stepping simulation** that advances the projected clock
through the charge and looks up the rate at each point in (SoC, clock) — not just
at the start. The backward variant finds the latest safe start for a deadline.

No heavy dependencies — pure Python sufficient statistics, persisted as JSON, and
degrades gracefully (no data → None → caller's configured fallback rate).
"""

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

STATS_FILE = Path("/data/charge_stats.json")

# Outlet power below this (W) is treated as idle/standby/ramp, not real charging.
MIN_CHARGING_W = 1400
# Cap on the persisted observation log (each ~one poll while charging).
MAX_OBS = 5000
# Persist after this many new samples to bound disk writes during long sessions.
_SAVE_EVERY = 20

# Minimum global samples before any learned estimate is offered.
_MIN_GLOBAL = 8
# Prior strength (pseudo-counts) for shrinkage. Higher = more data before a bin
# overrides the global estimate. Time effect here is strong+real, so shrink less.
_KAPPA_SOC = 8.0
_KAPPA_TIME = 5.0

# Safety margin: when computing the latest safe start, assume the rate could be
# this many standard deviations below the mean. The per-hour coefficient of
# variation sets how much margin each hour actually gets.
_SAFETY_Z = 1.0
# Prior coefficient of variation (std/mean) used until an hour has its own spread.
_DEFAULT_CV = 0.25
# Exceptional days are inherently less predictable; start them with more assumed
# spread so the safety margin is wider until they earn their own data.
_DEFAULT_CV_SPECIAL = 0.45
_KAPPA_CV = 5.0

# Day-class sentinel for exceptional days (holidays/eves/user-flagged); normal
# days use their weekday 0-6. Special-day load is pooled here and falls back to
# Sunday rather than the nominal weekday.
SPECIAL_DAY_CLASS = 7
_SUNDAY = 6

# A sample is "car-limited" when the car draws at least this many amps below the
# system-allowed ceiling (i.e. the car, not the load balancer, is the constraint).
_CAR_LIMIT_MARGIN_A = 2
# Floor on the conservative rate as a fraction of the mean (avoids absurd margins).
_MIN_CONSERVATIVE_FRAC = 0.2

# SoC buckets — finer near the top where AC charging tapers.
_SOC_EDGES = (0, 50, 70, 85, 93, 101)
# Time-of-day resolution: 1-hour buckets (24/day) to capture the evening→night
# load transition at full resolution. Sparse bins fall back via shrinkage (kappa).
_TIME_BUCKET_HOURS = 1

# Simulation timestep and a hard iteration cap (guards against tiny-rate runaway).
_SIM_STEP = timedelta(minutes=6)
_SIM_MAX_STEPS = 1000
# Floor on the per-step rate used in the duration sim. A real AC charge is never
# below ~1.4 kW (the recording gate), so anything tinier is a calibration glitch;
# without this floor a bad rate makes the latest-start land in the past and
# defeats just-in-time pausing.
_MIN_SANE_RATE_KW = 0.8


def _soc_bin(soc: Optional[float]) -> int:
    if soc is None:
        return -1
    for i in range(len(_SOC_EDGES) - 1):
        if _SOC_EDGES[i] <= soc < _SOC_EDGES[i + 1]:
            return i
    return len(_SOC_EDGES) - 2


_DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


@dataclass
class Observation:
    t: float          # epoch seconds
    hour: int         # 0-23
    weekday: int      # 0=Mon .. 6=Sun
    soc: Optional[int]  # 0-100 at time of sample, may be None
    power_w: int
    special: bool = False  # holiday / eve / user-flagged exceptional day
    amps: Optional[float] = None      # current the car is actually drawing (A)
    capped_a: Optional[int] = None    # static outlet rating (does NOT track throttle)
    max_a: Optional[int] = None       # hardware/cable max amps
    evse_max_a: Optional[int] = None  # live EVSE-offered current — the system limit

    @property
    def day_class(self) -> int:
        return SPECIAL_DAY_CLASS if self.special else self.weekday

    @property
    def car_revealing(self) -> Optional[bool]:
        """True when the delivered current reflects the car's own acceptance:
        either the system isn't throttling (offer >= max, so the car draws up to
        the hardware ceiling), or the car sits clearly below the offered current
        (high-SoC taper). Those are the samples that teach the car-acceptance curve."""
        if self.amps is None or self.evse_max_a is None or self.max_a is None:
            return None
        return self.evse_max_a >= self.max_a or self.amps < self.evse_max_a - _CAR_LIMIT_MARGIN_A

    def as_dict(self) -> dict:
        return {"t": self.t, "hour": self.hour, "weekday": self.weekday,
                "soc": self.soc, "power_w": self.power_w, "special": self.special,
                "amps": self.amps, "capped_a": self.capped_a, "max_a": self.max_a,
                "evse_max_a": self.evse_max_a}

    @classmethod
    def from_dict(cls, d: dict) -> "Observation":
        t = float(d["t"])
        # Backward-compat: older records stored `weekend` only; derive weekday from t.
        wd = int(d["weekday"]) if d.get("weekday") is not None \
            else datetime.fromtimestamp(t).weekday()
        _int = lambda v: int(v) if v is not None else None
        # Migrate amps stored in milliamps (early bug) to amps: real charge < 100 A.
        raw_a = d.get("amps")
        amps = (raw_a / 1000.0 if raw_a is not None and raw_a > 100 else raw_a)
        return cls(t=t, hour=int(d["hour"]), weekday=wd,
                   soc=(int(d["soc"]) if d.get("soc") is not None else None),
                   power_w=int(d["power_w"]), special=bool(d.get("special", False)),
                   amps=amps, capped_a=_int(d.get("capped_a")),
                   max_a=_int(d.get("max_a")), evse_max_a=_int(d.get("evse_max_a")))


class ChargeStats:
    """Context-tagged charging-power log plus a Bayesian-shrinkage rate model."""

    def __init__(self, path: Path = STATS_FILE,
                 is_special: Optional[Callable[[datetime], bool]] = None):
        self._path = path
        # Classifies a datetime as an exceptional (special) day; default: never.
        self._is_special = is_special or (lambda when: False)
        self._obs: List[Observation] = []
        self._unsaved = 0
        self._agg: Optional[dict] = None  # cached aggregates, invalidated on record
        self._load()

    def _day_class(self, when: datetime) -> int:
        return SPECIAL_DAY_CLASS if self._is_special(when) else when.weekday()

    # ── Persistence ───────────────────────────────────────────────────────────

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text())
            obs = raw.get("observations", [])
            self._obs = [Observation.from_dict(d) for d in obs[-MAX_OBS:]]
            rate = self.effective_rate_kw()
            log.info("Loaded %d charging-power samples (global rate %.1f kW)",
                     len(self._obs), rate or 0.0)
        except Exception as exc:
            log.warning("Could not load charge stats: %s", exc)

    def save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(
                {"observations": [o.as_dict() for o in self._obs[-MAX_OBS:]]}))
            self._unsaved = 0
        except Exception as exc:
            log.warning("Could not save charge stats: %s", exc)

    # ── Recording ─────────────────────────────────────────────────────────────

    def record(self, power_w: Optional[int], soc: Optional[float],
               when: Optional[datetime] = None, amps: Optional[int] = None,
               capped_amps: Optional[int] = None, max_amps: Optional[int] = None,
               evse_max_amps: Optional[int] = None) -> None:
        """Record an outlet power reading with context; ignored unless real charging."""
        if power_w is None or power_w < MIN_CHARGING_W:
            return
        when = when or datetime.now()
        self._obs.append(Observation(
            t=when.timestamp(), hour=when.hour, weekday=when.weekday(),
            soc=(int(round(soc)) if soc is not None else None), power_w=int(power_w),
            special=self._is_special(when),
            amps=(float(amps) if amps is not None else None),
            capped_a=(int(capped_amps) if capped_amps is not None else None),
            max_a=(int(max_amps) if max_amps is not None else None),
            evse_max_a=(int(evse_max_amps) if evse_max_amps is not None else None),
        ))
        if len(self._obs) > MAX_OBS:
            self._obs = self._obs[-MAX_OBS:]
        self._agg = None
        self._unsaved += 1
        if self._unsaved >= _SAVE_EVERY:
            self.save()

    # ── Aggregates (cached) ───────────────────────────────────────────────────

    def _aggregates(self) -> dict:
        """Precompute global / per-SoC-bin / per-time-bin means once per change."""
        if self._agg is not None:
            return self._agg
        soc_sum: Dict[int, float] = {}
        soc_sq: Dict[int, float] = {}
        soc_n: Dict[int, int] = {}
        time_sum: Dict[Tuple[int, int], float] = {}
        time_sq: Dict[Tuple[int, int], float] = {}
        time_n: Dict[Tuple[int, int], int] = {}
        # Disentangled curves (need amps/capped data):
        #  • car acceptance: delivered power in car-limited samples, per SoC bin
        #  • system availability: allowed amps per time bin
        car_sum: Dict[int, float] = {}
        car_n: Dict[int, int] = {}
        cap_sum: Dict[Tuple[int, int], float] = {}
        cap_n: Dict[Tuple[int, int], int] = {}
        vpa_pw = 0.0   # Σ power_w over calibration samples (amps>0)
        vpa_a = 0.0    # Σ amps   over the same samples → V/A = vpa_pw / vpa_a
        total = 0.0
        for o in self._obs:
            total += o.power_w
            sq = float(o.power_w) * o.power_w
            sb = _soc_bin(o.soc)
            if sb >= 0:
                soc_sum[sb] = soc_sum.get(sb, 0.0) + o.power_w
                soc_sq[sb] = soc_sq.get(sb, 0.0) + sq
                soc_n[sb] = soc_n.get(sb, 0) + 1
            tk = (o.day_class, o.hour // _TIME_BUCKET_HOURS)
            time_sum[tk] = time_sum.get(tk, 0.0) + o.power_w
            time_sq[tk] = time_sq.get(tk, 0.0) + sq
            time_n[tk] = time_n.get(tk, 0) + 1
            if o.amps:  # truthy and non-zero
                vpa_pw += o.power_w
                vpa_a += o.amps
            if o.evse_max_a is not None:  # live system-offered current (the throttle)
                cap_sum[tk] = cap_sum.get(tk, 0.0) + o.evse_max_a
                cap_n[tk] = cap_n.get(tk, 0) + 1
            if o.car_revealing and sb >= 0:
                car_sum[sb] = car_sum.get(sb, 0.0) + o.power_w
                car_n[sb] = car_n.get(sb, 0) + 1
        n = len(self._obs)
        self._agg = {
            "n": n,
            "global": (total / n) if n else None,
            "soc_sum": soc_sum, "soc_sq": soc_sq, "soc_n": soc_n,
            "time_sum": time_sum, "time_sq": time_sq, "time_n": time_n,
            "car_sum": car_sum, "car_n": car_n,
            "cap_sum": cap_sum, "cap_n": cap_n,
            "volts_per_amp": (vpa_pw / vpa_a) if vpa_a > 0 else None,
        }
        return self._agg

    # ── Model ─────────────────────────────────────────────────────────────────

    def _global_mean_w(self) -> Optional[float]:
        a = self._aggregates()
        return a["global"] if a["n"] >= _MIN_GLOBAL else None

    def effective_rate_kw(self) -> Optional[float]:
        """Overall learned charging power in kW, or None if not enough data."""
        g = self._global_mean_w()
        return g / 1000.0 if g is not None else None

    @staticmethod
    def _std(n: int, s: float, sq: float) -> Optional[float]:
        """Sample std-dev from count / sum / sum-of-squares, or None if n < 2."""
        if n < 2:
            return None
        var = sq / n - (s / n) ** 2
        return var ** 0.5 if var > 0 else 0.0

    def _shrunk_time_mean_w(self, day_class: int, bucket: int, parent_w: float) -> float:
        """Mean power for a (day_class, hour) bin, shrunk toward `parent_w`."""
        a = self._aggregates()
        key = (day_class, bucket)
        m = a["time_n"].get(key, 0)
        bin_mean = (a["time_sum"][key] / m) if m else parent_w
        return (_KAPPA_TIME * parent_w + m * bin_mean) / (_KAPPA_TIME + m)

    def _time_mean_w(self, day_class: int, bucket: int, mu0: float) -> float:
        """Time-bin mean with hierarchical back-off. Special days fall back to
        Sunday then global; normal days fall back straight to global."""
        if day_class == SPECIAL_DAY_CLASS:
            sunday_w = self._shrunk_time_mean_w(_SUNDAY, bucket, mu0)
            return self._shrunk_time_mean_w(SPECIAL_DAY_CLASS, bucket, sunday_w)
        return self._shrunk_time_mean_w(day_class, bucket, mu0)

    def _time_cv(self, day_class: int, bucket: int) -> float:
        """Coefficient of variation for a (day_class, hour) bin, shrunk toward a
        default. Exceptional days assume more spread until they have their own."""
        a = self._aggregates()
        key = (day_class, bucket)
        m = a["time_n"].get(key, 0)
        std = self._std(m, a["time_sum"].get(key, 0.0), a["time_sq"].get(key, 0.0))
        mean = (a["time_sum"][key] / m) if m else 0.0
        default_cv = _DEFAULT_CV_SPECIAL if day_class == SPECIAL_DAY_CLASS else _DEFAULT_CV
        cv_obs = (std / mean) if (std is not None and mean > 0) else default_cv
        return (_KAPPA_CV * default_cv + m * cv_obs) / (_KAPPA_CV + m)

    # ── Forward model: min(car acceptance(soc), system available(time)) ────────

    def _car_accept_w(self, soc: Optional[float]) -> Optional[float]:
        """Car's accepted power (W) at this SoC, learned only from car-limited
        samples (so it's the true taper, unconfounded by system throttling)."""
        a = self._aggregates()
        tot_n = sum(a["car_n"].values())
        if tot_n < _MIN_GLOBAL:
            return None
        parent = sum(a["car_sum"].values()) / tot_n
        sb = _soc_bin(soc)
        n = a["car_n"].get(sb, 0) if sb >= 0 else 0
        bin_mean = (a["car_sum"][sb] / n) if n else parent
        return (_KAPPA_SOC * parent + n * bin_mean) / (_KAPPA_SOC + n)

    def _cap_amps_for(self, when: datetime, margin_z: float) -> Optional[float]:
        """System-allowed amps for this clock context (hierarchical back-off for
        special days), optionally with a conservative margin from its own spread."""
        a = self._aggregates()
        cap_n, cap_sum = a["cap_n"], a["cap_sum"]
        tot_n = sum(cap_n.values())
        if tot_n < _MIN_GLOBAL:
            return None
        parent = sum(cap_sum.values()) / tot_n

        def shrunk(day_class: int, bucket: int, par: float) -> float:
            key = (day_class, bucket)
            m = cap_n.get(key, 0)
            mean = (cap_sum[key] / m) if m else par
            return (_KAPPA_TIME * par + m * mean) / (_KAPPA_TIME + m)

        dc = self._day_class(when)
        bucket = when.hour // _TIME_BUCKET_HOURS
        if dc == SPECIAL_DAY_CLASS:
            cap = shrunk(SPECIAL_DAY_CLASS, bucket, shrunk(_SUNDAY, bucket, parent))
        else:
            cap = shrunk(dc, bucket, parent)

        if margin_z:
            key = (dc, bucket)
            m = cap_n.get(key, 0)
            std = self._std(m, cap_sum.get(key, 0.0),
                            a.get("cap_sq", {}).get(key, 0.0)) if m else None
            # Without per-bin variance for caps, fall back to the delivered-power CV.
            cv = (std / cap) if (std and cap > 0) else self._time_cv(dc, bucket)
            cap *= max(1.0 - margin_z * cv, _MIN_CONSERVATIVE_FRAC)
        return cap

    def _forward_kw(self, soc: Optional[float], when: Optional[datetime],
                    margin_z: float) -> Optional[float]:
        """min(car acceptance, system availability) in kW, or None unless BOTH
        curves have data (else the caller uses the combined-delivered model)."""
        vpa = self._aggregates()["volts_per_amp"]
        car_w = self._car_accept_w(soc)
        if vpa is None or car_w is None or when is None:
            return None
        cap_a = self._cap_amps_for(when, margin_z)
        if cap_a is None:
            return None
        system_w = cap_a * vpa
        return min(car_w, system_w) / 1000.0

    def rate_kw_for(self, soc: Optional[float], when: Optional[datetime] = None,
                    margin_z: float = 0.0) -> Optional[float]:
        """
        Charging power (kW) for the given SoC and clock context.

        Preferred path is the forward model min(car_acceptance(soc),
        system_available(time)) once both curves have data. Until then it falls
        back to the combined-delivered model: the SoC bin shrunk toward the global
        mean, times a time-of-day load factor (special days back off to Sunday).
        margin_z > 0 applies a conservative per-hour-variability haircut. Returns
        None when there is not yet enough data to learn anything.
        """
        forward = self._forward_kw(soc, when, margin_z)
        if forward is not None:
            return forward

        mu0 = self._global_mean_w()
        if mu0 is None:
            return None
        a = self._aggregates()

        # Base rate: SoC bin, shrunk toward the global mean by pseudo-count kappa.
        sb = _soc_bin(soc)
        n = a["soc_n"].get(sb, 0) if sb >= 0 else 0
        bin_mean = (a["soc_sum"][sb] / n) if n else mu0
        base_w = (_KAPPA_SOC * mu0 + n * bin_mean) / (_KAPPA_SOC + n)

        # Time-of-day load factor with hierarchical back-off (special → Sunday →
        # global for exceptional days; weekday → global otherwise).
        rate_w = base_w
        if when is not None:
            day_class = self._day_class(when)
            bucket = when.hour // _TIME_BUCKET_HOURS
            rate_w = base_w * (self._time_mean_w(day_class, bucket, mu0) / mu0)

            # Safety margin: assume the rate could sit margin_z CVs below the mean.
            if margin_z:
                haircut = max(1.0 - margin_z * self._time_cv(day_class, bucket),
                              _MIN_CONSERVATIVE_FRAC)
                rate_w *= haircut

        return rate_w / 1000.0

    # ── Duration via time-stepping simulation ─────────────────────────────────

    def _dsoc(self, rate_kw: float, dt: timedelta, capacity_kwh: float, efficiency: float) -> float:
        """SoC percentage gained over dt at rate_kw (AC), accounting for efficiency."""
        energy_ac = rate_kw * (dt.total_seconds() / 3600.0)
        return energy_ac * efficiency / capacity_kwh * 100.0

    def done_at(self, soc_from: float, soc_to: float, start: datetime,
                capacity_kwh: float, efficiency: float, fallback_rate_kw: float,
                margin_z: float = 0.0) -> datetime:
        """Project when soc_to is reached, advancing the clock so the rate tracks
        the time-of-day load profile across the charge. margin_z>0 gives a
        conservative (later) estimate."""
        if soc_to <= soc_from:
            return start
        clock = start
        soc = float(soc_from)
        for _ in range(_SIM_MAX_STEPS):
            rate = self.rate_kw_for(soc, clock, margin_z) or fallback_rate_kw
            soc += self._dsoc(max(rate, _MIN_SANE_RATE_KW), _SIM_STEP, capacity_kwh, efficiency)
            clock += _SIM_STEP
            if soc >= soc_to:
                break
        return clock

    def latest_start(self, soc_from: float, soc_to: float, deadline: datetime,
                     capacity_kwh: float, efficiency: float, fallback_rate_kw: float,
                     margin_z: float = _SAFETY_Z) -> datetime:
        """Latest start so soc_to is reached by `deadline`, stepping backward in
        time from the deadline so the rate tracks the load profile in reverse.
        Defaults to a conservative rate (margin_z CVs below mean) so a noisy hour
        triggers an earlier, safer start."""
        if soc_to <= soc_from:
            return deadline
        clock = deadline
        soc = float(soc_to)
        for _ in range(_SIM_MAX_STEPS):
            rate = self.rate_kw_for(soc, clock, margin_z) or fallback_rate_kw
            soc -= self._dsoc(max(rate, _MIN_SANE_RATE_KW), _SIM_STEP, capacity_kwh, efficiency)
            clock -= _SIM_STEP
            if soc <= soc_from:
                break
        return clock

    @property
    def sample_count(self) -> int:
        return len(self._obs)

    # ── Inspection ────────────────────────────────────────────────────────────

    def summary(self) -> dict:
        """Human-readable breakdown of the learned model for inspection."""
        a = self._aggregates()
        g = a["global"]

        vpa = a["volts_per_amp"]
        by_soc = []
        for sb in range(len(_SOC_EDGES) - 1):
            n = a["soc_n"].get(sb, 0)
            std = self._std(n, a["soc_sum"].get(sb, 0.0), a["soc_sq"].get(sb, 0.0))
            cn = a["car_n"].get(sb, 0)
            by_soc.append({
                "range": f"{_SOC_EDGES[sb]}-{_SOC_EDGES[sb + 1] - 1}%",
                "n": n,
                "mean_kw": round(a["soc_sum"][sb] / n / 1000, 2) if n else None,
                "std_kw": round(std / 1000, 2) if std is not None else None,
                # Car acceptance: delivered power in samples that reveal the car's
                # own demand (system not throttling, or car below the cap).
                "car_revealing_n": cn,
                "car_accept_kw": round(a["car_sum"][sb] / cn / 1000, 2) if cn else None,
            })

        # Populated (day_class, hour) cells only, so sparse data stays readable.
        # day_class 7 is the pooled "Special" (holiday/eve/flagged) bucket.
        by_day_hour = []
        for (day_class, bucket), n in sorted(a["time_n"].items()):
            key = (day_class, bucket)
            mean = a["time_sum"][key] / n
            std = self._std(n, a["time_sum"][key], a["time_sq"][key])
            day = "Special" if day_class == SPECIAL_DAY_CLASS else _DAY_NAMES[day_class]
            cn = a["cap_n"].get(key, 0)  # system-offered amps (evse_max) per bin
            cap_a = (a["cap_sum"][key] / cn) if cn else None
            by_day_hour.append({
                "day_class": day_class,
                "day": day,
                "hour": bucket * _TIME_BUCKET_HOURS,
                "n": n,
                "mean_kw": round(mean / 1000, 2),
                "std_kw": round(std / 1000, 2) if std is not None else None,
                "cv": round(std / mean, 2) if (std is not None and mean > 0) else None,
                "safety_kw": round(mean * max(1.0 - _SAFETY_Z * self._time_cv(day_class, bucket),
                                              _MIN_CONSERVATIVE_FRAC) / 1000, 2),
                # System availability = mean EVSE-offered current (the live throttle).
                "system_offered_amps": round(cap_a, 1) if cap_a is not None else None,
                "system_avail_kw": round(cap_a * vpa / 1000, 2) if (cap_a is not None and vpa) else None,
            })

        return {
            "samples": a["n"],
            "global_rate_kw": round(g / 1000, 2) if g is not None else None,
            "min_samples_for_estimate": _MIN_GLOBAL,
            "volts_per_amp": round(vpa, 1) if vpa else None,
            "forward_model_active": vpa is not None
                and sum(a["car_n"].values()) >= _MIN_GLOBAL
                and sum(a["cap_n"].values()) >= _MIN_GLOBAL,
            "by_soc": by_soc,
            "by_day_hour": by_day_hour,
        }
