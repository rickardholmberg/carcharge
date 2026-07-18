"""
Exceptional-day calendar.

Charging-load behaviour on holidays and their eves differs sharply from a normal
day of the same weekday, but each such day recurs only ~once a year, so it can
never be learned from its own data. We therefore classify these days so the rate
model can pool them into a single "special" day-type that falls back to Sunday-
like behaviour (see stats.ChargeStats), and so their samples don't pollute the
regular weekday bins.

Two sources, unioned:
  • Automatic — Swedish public holidays via the `holidays` library, plus their
    **eves** (the day before a named holiday), which captures Julafton,
    Midsommarafton, Valborg, Nyårsafton, Påskafton, … without hardcoding dates.
    The library marks every Sunday as a holiday; those are filtered out.
  • Manual — a user-editable list at /data/special_days.json for days the
    calendar can't know (e.g. the last day of school term).
"""

import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

import holidays as _holidays

log = logging.getLogger(__name__)

SPECIAL_DAYS_FILE = Path("/data/special_days.json")


class SpecialDays:
    def __init__(self, country: str = "SE", user_file: Path = SPECIAL_DAYS_FILE):
        self._country = country
        self._user_file = user_file
        self._user: Dict[date, str] = {}
        self._hol_cache: Dict[int, Dict[date, str]] = {}
        self._load_user()

    # ── Manual list persistence ───────────────────────────────────────────────

    def _load_user(self) -> None:
        if not self._user_file.exists():
            return
        try:
            raw = json.loads(self._user_file.read_text())
            self._user = {date.fromisoformat(e["date"]): e.get("label", "special")
                          for e in raw}
            log.info("Loaded %d user special-days", len(self._user))
        except Exception as exc:
            log.warning("Could not load special-days file: %s", exc)

    def _save_user(self) -> None:
        try:
            self._user_file.parent.mkdir(parents=True, exist_ok=True)
            raw = [{"date": d.isoformat(), "label": lbl}
                   for d, lbl in sorted(self._user.items())]
            self._user_file.write_text(json.dumps(raw, indent=2))
        except Exception as exc:
            log.warning("Could not save special-days file: %s", exc)

    def add(self, d: date, label: str) -> None:
        self._user[d] = label
        self._save_user()

    def remove(self, d: date) -> bool:
        if d in self._user:
            del self._user[d]
            self._save_user()
            return True
        return False

    def list_user(self) -> List[dict]:
        return [{"date": d.isoformat(), "label": lbl}
                for d, lbl in sorted(self._user.items())]

    # ── Holiday lookup ────────────────────────────────────────────────────────

    def _holidays_for(self, year: int) -> Dict[date, str]:
        """Named Swedish holidays for `year`, with plain Sundays filtered out.

        Sweden's data lists every Sunday as a holiday; that generic entry is the
        one name that recurs ~52×/year, so we detect it by frequency (rather than
        a hard-coded "Sunday"/"Söndag" string) and strip it — locale-independent.
        """
        if year not in self._hol_cache:
            try:
                raw = dict(_holidays.country_holidays(self._country, years=year))
            except Exception as exc:
                log.warning("Holiday lookup failed for %d: %s", year, exc)
                self._hol_cache[year] = {}
                return self._hol_cache[year]

            # The generic weekly-Sunday label is whatever name recurs most on Sundays.
            sunday_counts: Dict[str, int] = {}
            for d, name in raw.items():
                if d.weekday() == 6:
                    sunday_counts[name] = sunday_counts.get(name, 0) + 1
            generic = max(sunday_counts, key=sunday_counts.get, default=None)
            if generic is not None and sunday_counts[generic] < 20:
                generic = None  # not the weekly-Sunday quirk; keep everything

            cleaned: Dict[date, str] = {}
            for d, name in raw.items():
                parts = [p.strip() for p in name.split(";") if p.strip() != generic]
                if parts:  # something other than the plain weekly Sunday remains
                    cleaned[d] = "; ".join(parts)
            self._hol_cache[year] = cleaned
        return self._hol_cache[year]

    def _named_holiday(self, d: date) -> Optional[str]:
        return self._holidays_for(d.year).get(d)

    # ── Public classification ─────────────────────────────────────────────────

    def label(self, d: date) -> Optional[str]:
        """Return a label if `d` is exceptional (manual > holiday > eve), else None."""
        if d in self._user:
            return self._user[d]
        named = self._named_holiday(d)
        if named:
            return named
        eve_of = self._named_holiday(d + timedelta(days=1))
        if eve_of:
            return f"{eve_of} eve"
        return None

    def is_special(self, d: date) -> bool:
        return self.label(d) is not None

    def is_special_dt(self, when: datetime) -> bool:
        return self.is_special(when.date())

    def upcoming(self, start: date, days: int = 60) -> List[dict]:
        """Exceptional days within the next `days`, for display."""
        out = []
        for i in range(days):
            d = start + timedelta(days=i)
            lbl = self.label(d)
            if lbl:
                out.append({"date": d.isoformat(), "label": lbl,
                            "manual": d in self._user})
        return out
