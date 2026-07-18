"""Trip persistence: load/save the manually added trip list from/to /data/trips.json."""

import json
import logging
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

TRIPS_FILE = Path("/data/trips.json")


def load_trips() -> None:
    from .state import state
    if not TRIPS_FILE.exists():
        return
    try:
        raw = json.loads(TRIPS_FILE.read_text())
        now = datetime.now()
        state.trips = [
            {
                "id": t["id"],
                "depart_at": datetime.fromisoformat(t["depart_at"]),
                "soc_pct": t.get("soc_pct", 90),
            }
            for t in raw
            if datetime.fromisoformat(t["depart_at"]) > now
        ]
    except Exception as exc:
        log.warning("Could not load trips file: %s", exc)


def save_trips() -> None:
    from .state import state
    try:
        TRIPS_FILE.parent.mkdir(parents=True, exist_ok=True)
        raw = [{"id": t["id"], "depart_at": t["depart_at"].isoformat(), "soc_pct": t.get("soc_pct", 90)} for t in state.trips]
        TRIPS_FILE.write_text(json.dumps(raw, indent=2))
    except Exception as exc:
        log.warning("Could not save trips file: %s", exc)


def cleanup_past_trips() -> None:
    """Remove trips whose departure is in the past; save if anything was removed."""
    from .state import state
    now = datetime.now()
    before = len(state.trips)
    state.trips = [t for t in state.trips if t["depart_at"] > now]
    if len(state.trips) < before:
        save_trips()
