"""Minimal status web UI served by aiohttp."""

import json
import math
import os
import uuid
from datetime import date, datetime, timedelta
from typing import Optional

from aiohttp import web

from .state import state
from .trips import save_trips

_web_base_path: Optional[str] = None


def web_base_path() -> str:
    """External URL prefix when served behind a reverse proxy (e.g. /carcharge)."""
    global _web_base_path
    if _web_base_path is None:
        _web_base_path = os.environ.get("WEB_BASE_PATH", "").strip().rstrip("/")
    return _web_base_path


def _render_html(template: str) -> str:
    return template.replace("__BASE__", web_base_path())


def _distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6_371_000.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    )
    return 2 * R * math.asin(math.sqrt(a))


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


def _fmt_dt(dt: Optional[datetime]) -> Optional[str]:
    """Human-readable datetime: 'today 07:30' or 'Thu 07:30'."""
    if not dt:
        return None
    now = datetime.now()
    time_s = dt.strftime("%H:%M")
    if dt.date() == now.date():
        return f"today {time_s}"
    if dt.date() == (now + timedelta(days=1)).date():
        return f"tomorrow {time_s}"
    return dt.strftime("%a %H:%M")


def _fmt_trip_dt(dt: Optional[datetime]) -> Optional[str]:
    """Human-readable datetime for trips, includes explicit date: 'today 07:30' / 'Mon Jun 9 07:30'."""
    if not dt:
        return None
    now = datetime.now()
    time_s = dt.strftime("%H:%M")
    if dt.date() == now.date():
        return f"today {time_s}"
    if dt.date() == (now + timedelta(days=1)).date():
        return f"tomorrow {time_s}"
    return dt.strftime("%a %-d %b %H:%M")


def _fmt_age(dt: Optional[datetime]) -> Optional[str]:
    """'42s ago', '5m ago', '2h ago'."""
    if not dt:
        return None
    secs = max(0, round((datetime.now() - dt).total_seconds()))
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    return f"{secs // 3600}h ago"


def _est_done(soc: Optional[float]) -> Optional[str]:
    """
    Estimate when the target SoC will be reached from now,
    based on current SoC and configured charge rate.
    Returns a formatted string or None if not computable.
    """
    if soc is None:
        return None
    target = state.target_soc_pct
    if soc >= target:
        return "already at target"
    now = datetime.now()
    if state.charge_stats is not None:
        done_at = state.charge_stats.done_at(
            soc, target, now,
            capacity_kwh=state.battery_capacity_kwh,
            efficiency=state.charging_efficiency,
            fallback_rate_kw=state.charge_rate_kw,
        )
    else:
        energy_kwh = (target - soc) / 100.0 * state.battery_capacity_kwh / state.charging_efficiency
        done_at = now + timedelta(hours=energy_kwh / state.charge_rate_kw)
    return _fmt_dt(done_at)


async def handle_status_json(request: web.Request) -> web.Response:
    dist: Optional[int] = None
    at_charger: Optional[bool] = None
    if state.mercedes_lat and state.mercedes_lon and state.charger_lat and state.charger_lon:
        dist = round(_distance_m(
            state.mercedes_lat, state.mercedes_lon, state.charger_lat, state.charger_lon
        ))
        at_charger = dist <= 150

    plug = state.outlet_plug_inserted
    power_w = state.outlet_power_w or 0
    if plug and state.active_session_id:
        outlet_state = "charging" if power_w > 0 else "session active"
    elif plug:
        outlet_state = "plugged in"
    else:
        outlet_state = "empty"

    data = {
        "epspot": {
            "ok": state.epspot_ok,
            "user": state.epspot_user,
            "balance_sek": round(state.epspot_balance_sek, 2) if state.epspot_balance_sek is not None else None,
            "outlet_state": outlet_state,
            "outlet_power_w": power_w,
            "outlet_amps": round(state.outlet_amps, 1) if state.outlet_amps is not None else None,
            "evse_offered_amps": state.outlet_evse_max_amps,
            "system_max_amps": state.outlet_max_amps,
            "limit_source": state.outlet_limit_source,
            "system_throttled": (state.outlet_evse_max_amps is not None
                                 and state.outlet_max_amps is not None
                                 and state.outlet_evse_max_amps < state.outlet_max_amps),
            "active_session_id": state.active_session_id,
            "active_session_started": _fmt_dt(state.active_session_started),
            "active_session_kwh": round(state.active_session_kwh, 2) if state.active_session_kwh else None,
            "last_poll_age": _fmt_age(state.last_epspot_poll),
        },
        "mercedes": {
            "ok": state.mercedes_ok,
            "soc": round(state.mercedes_soc) if state.mercedes_soc is not None else None,
            "charging": state.mercedes_charging,
            "distance_m": dist,
            "at_charger": at_charger,
            "gps": f"{state.mercedes_lat:.5f}, {state.mercedes_lon:.5f}"
                   if state.mercedes_lat is not None else None,
            "updated_age": _fmt_age(state.mercedes_updated_at),
        },
        "scheduler": {
            "basic_soc_pct": state.basic_soc_pct,
            "trip_soc_pct": state.trip_soc_pct,
            "target_soc_pct": state.target_soc_pct,
            "in_trip_mode": state.target_soc_pct == state.trip_soc_pct,
            "trip_pending": state.trip_pending,
            "trip_pending_target": state.trip_pending_target,
            "charge_starts": _fmt_dt(state.next_charge_start),
            "ready_by": _fmt_dt(state.next_departure),
            "est_done_if_charging_now": _est_done(state.mercedes_soc),
            "climate_prep_at": _fmt_dt(state.climate_prep_at),
            "charge_rate_kw": round(state.learned_charge_rate_kw, 1)
                              if state.learned_charge_rate_kw else None,
            "charge_rate_kw_configured": round(state.charge_rate_kw, 1),
            "charge_samples": state.charge_samples,
        },
        "trips": [
            {
                "id": t["id"],
                "label": _fmt_trip_dt(t["depart_at"]),
                "iso": t["depart_at"].isoformat(),
                "soc_pct": t.get("soc_pct", state.trip_soc_pct),
            }
            for t in sorted(state.trips, key=lambda t: t["depart_at"])
        ],
        "last_error": state.last_error,
    }
    return web.Response(
        text=json.dumps(data, indent=2),
        content_type="application/json",
    )


async def handle_index(request: web.Request) -> web.Response:
    return web.Response(text=_render_html(_HTML), content_type="text/html")


async def handle_stats_page(request: web.Request) -> web.Response:
    return web.Response(text=_render_html(_STATS_HTML), content_type="text/html")


async def handle_trip_add(request: web.Request) -> web.Response:
    try:
        body = await request.json()
        depart_at = datetime.fromisoformat(body["depart_at"])
        soc_pct = max(50, min(100, int(body.get("soc_pct", state.trip_soc_pct))))
    except Exception:
        return web.Response(status=400, text='Invalid body; expected {"depart_at": "ISO8601", "soc_pct": 90}')
    if depart_at <= datetime.now():
        return web.Response(status=400, text="Departure must be in the future")
    state.trips.append({"id": str(uuid.uuid4()), "depart_at": depart_at, "soc_pct": soc_pct})
    state.trips.sort(key=lambda t: t["depart_at"])
    save_trips()
    return web.Response(status=201, text="ok")


async def handle_trip_delete(request: web.Request) -> web.Response:
    trip_id = request.match_info["trip_id"]
    before = len(state.trips)
    state.trips = [t for t in state.trips if t["id"] != trip_id]
    if len(state.trips) == before:
        return web.Response(status=404, text="Not found")
    save_trips()
    return web.Response(status=200, text="ok")


async def handle_stats_json(request: web.Request) -> web.Response:
    s = state.charge_stats
    data = s.summary() if s is not None else {"samples": 0}
    return web.Response(text=json.dumps(data, indent=2), content_type="application/json")


async def handle_special_days_get(request: web.Request) -> web.Response:
    sd = state.special_days
    if sd is None:
        return web.Response(text=json.dumps({"manual": [], "upcoming": []}),
                            content_type="application/json")
    data = {"manual": sd.list_user(), "upcoming": sd.upcoming(date.today(), 90)}
    return web.Response(text=json.dumps(data, indent=2), content_type="application/json")


async def handle_special_day_add(request: web.Request) -> web.Response:
    sd = state.special_days
    if sd is None:
        return web.Response(status=503, text="Special-days not ready")
    try:
        body = await request.json()
        d = date.fromisoformat(body["date"])
        label = str(body.get("label", "special")).strip() or "special"
    except Exception:
        return web.Response(status=400, text='Invalid body; expected {"date": "YYYY-MM-DD", "label": "..."}')
    sd.add(d, label)
    return web.Response(status=201, text="ok")


async def handle_special_day_delete(request: web.Request) -> web.Response:
    sd = state.special_days
    if sd is None:
        return web.Response(status=503, text="Special-days not ready")
    try:
        d = date.fromisoformat(request.match_info["date"])
    except ValueError:
        return web.Response(status=400, text="Invalid date")
    if not sd.remove(d):
        return web.Response(status=404, text="Not found")
    return web.Response(status=200, text="ok")


def create_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_get("/stats", handle_stats_page)
    app.router.add_get("/api/status", handle_status_json)
    app.router.add_get("/api/stats", handle_stats_json)
    app.router.add_post("/api/trips", handle_trip_add)
    app.router.add_delete("/api/trips/{trip_id}", handle_trip_delete)
    app.router.add_get("/api/special-days", handle_special_days_get)
    app.router.add_post("/api/special-days", handle_special_day_add)
    app.router.add_delete("/api/special-days/{date}", handle_special_day_delete)
    return app


# ── HTML ──────────────────────────────────────────────────────────────────────

_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>carcharge</title>
<style>
  :root {
    --bg: #0f1117; --card: #1a1d27; --border: #2d3044;
    --text: #e2e4f0; --muted: #7b7f9e; --green: #4ade80;
    --red: #f87171; --yellow: #fbbf24; --accent: #818cf8;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: system-ui, sans-serif;
         font-size: 14px; padding: 24px; }
  h1 { font-size: 20px; font-weight: 600; color: var(--accent); margin-bottom: 20px; }
  h1 span { color: var(--muted); font-weight: 400; font-size: 13px; margin-left: 10px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 16px; }
  .card { background: var(--card); border: 1px solid var(--border); border-radius: 12px;
          padding: 18px; }
  .card-title { font-size: 11px; font-weight: 700; text-transform: uppercase;
                letter-spacing: .08em; color: var(--muted); margin-bottom: 14px; }
  .row { display: flex; justify-content: space-between; align-items: center;
         padding: 5px 0; border-bottom: 1px solid var(--border); }
  .row:last-child { border-bottom: none; }
  .label { color: var(--muted); }
  .val { font-weight: 500; }
  .dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; margin-right: 6px; }
  .dot-green  { background: var(--green);  box-shadow: 0 0 6px var(--green); }
  .dot-red    { background: var(--red);    box-shadow: 0 0 6px var(--red); }
  .dot-yellow { background: var(--yellow); box-shadow: 0 0 6px var(--yellow); }
  .dot-grey   { background: var(--muted); }
  .soc-wrap { position: relative; margin-top: 12px; }
  .soc-bar { width: 100%; height: 8px; background: var(--border); border-radius: 4px; overflow: visible; }
  .soc-fill { height: 100%; border-radius: 4px; transition: width .4s ease; }
  .soc-target { position: absolute; top: -3px; width: 2px; height: 14px;
                border-radius: 1px; }
  .soc-label { display: flex; justify-content: space-between; margin-top: 4px;
               font-size: 11px; color: var(--muted); }
  .badge { font-size: 10px; font-weight: 700; padding: 1px 6px; border-radius: 4px;
           text-transform: uppercase; letter-spacing: .05em; }
  .badge-trip { background: rgba(129,140,248,.2); color: var(--accent); }
  .badge-basic { background: rgba(123,127,158,.15); color: var(--muted); }
  .del-btn { background: none; border: 1px solid var(--border); color: var(--muted);
             border-radius: 6px; cursor: pointer; font-size: 11px; padding: 2px 7px;
             line-height: 1.4; }
  .del-btn:hover { border-color: var(--red); color: var(--red); }
  .trip-form { display: flex; gap: 8px; margin-top: 14px; }
  .trip-form input { flex: 1; background: var(--bg); border: 1px solid var(--border);
                     color: var(--text); border-radius: 6px; padding: 5px 8px;
                     font-size: 13px; min-width: 0; }
  .trip-form button { background: var(--accent); border: none; color: #fff;
                      border-radius: 6px; padding: 5px 12px; cursor: pointer;
                      font-size: 13px; white-space: nowrap; }
  .trip-form button:hover { opacity: .85; }
  .error { color: var(--red); font-size: 12px; margin-top: 14px; word-break: break-all; }
  .footer { color: var(--muted); font-size: 11px; margin-top: 20px; }
  a.navlink { color: var(--accent); text-decoration: none; font-size: 13px; font-weight: 500; margin-left: 14px; }
  a.navlink:hover { text-decoration: underline; }
</style>
<script>
function api(p) {
  const base = '__BASE__';
  const path = p.replace(/^\\//, '');
  return base ? base + '/' + path : '/' + path;
}
</script>
</head>
<body>
<h1>⚡ carcharge <span id="ts"></span><a href="__BASE__/stats" class="navlink">stats &amp; details →</a></h1>
<div class="grid" id="grid">
  <div class="card"><div class="card-title">Loading…</div></div>
</div>
<div class="footer">Auto-refreshes every 30 s</div>

<script>
function row(label, val) {
  return `<div class="row"><span class="label">${label}</span><span class="val">${val ?? '—'}</span></div>`;
}
function dot(ok) {
  if (ok === null || ok === undefined) return `<span class="dot dot-grey"></span>`;
  return ok ? `<span class="dot dot-green"></span>` : `<span class="dot dot-red"></span>`;
}
function limitBadge(src) {
  if (src === 'system') return '<span style="color:var(--yellow)">system throttle</span>';
  if (src === 'car')    return '<span style="color:var(--muted)">car (taper)</span>';
  if (src === 'full')   return '<span style="color:var(--green)">full power</span>';
  return '—';
}

async function refresh() {
  try {
    const r = await fetch(api('api/status'));
    if (!r.ok) throw new Error(`API ${r.status}`);
    const d = await r.json();
    render(d);
    document.getElementById('ts').textContent = new Date().toLocaleTimeString('sv-SE');
  } catch(e) {
    document.getElementById('ts').textContent = 'error: ' + e;
  }
}

async function addTrip() {
  const inp = document.getElementById('trip-dt');
  const soc = parseInt(document.getElementById('trip-soc').value) || 90;
  if (!inp.value) return;
  const r = await fetch(api('api/trips'), {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({depart_at: inp.value, soc_pct: soc}),
  });
  if (r.ok) { inp.value = ''; refresh(); }
}

async function deleteTrip(id) {
  await fetch(api('api/trips/' + id), {method: 'DELETE'});
  refresh();
}

function render(d) {
  const ep = d.epspot, mb = d.mercedes, sc = d.scheduler;

  const outletDot = {
    'charging':        '<span class="dot dot-green"></span>',
    'session active':  '<span class="dot dot-yellow"></span>',
    'plugged in':      '<span class="dot dot-yellow"></span>',
    'empty':           '<span class="dot dot-grey"></span>',
  };
  const outletStateStr = ep.outlet_state;
  const outletLabel = (outletDot[ep.outlet_state] ?? '<span class="dot dot-grey"></span>') + (outletStateStr ?? '—');

  const distLabel = mb.distance_m === null ? '—'
    : mb.at_charger
      ? `<span style="color:var(--green)">${mb.distance_m} m ✓</span>`
      : `${mb.distance_m} m`;

  const soc = mb.soc;
  const socColor = soc === null ? 'var(--muted)'
    : soc >= 80 ? 'var(--green)' : soc >= 40 ? 'var(--yellow)' : 'var(--red)';
  const basicPct = sc.basic_soc_pct;
  const tripPct  = sc.trip_soc_pct;
  const tripMark = sc.in_trip_mode
    ? `<div class="soc-target" style="left:${tripPct}%;background:var(--accent)" title="Trip ${tripPct}%"></div>` : '';
  const targetLbl = sc.in_trip_mode
    ? `basic ${basicPct}% · <span style="color:var(--accent)">trip ${tripPct}%</span>`
    : `target ${basicPct}%`;
  const socBar = soc !== null ? `
    <div class="soc-wrap">
      <div class="soc-bar">
        <div class="soc-fill" style="width:${soc}%;background:${socColor}"></div>
      </div>
      <div class="soc-target" style="left:${basicPct}%;background:var(--muted)" title="Basic ${basicPct}%"></div>
      ${tripMark}
      <div class="soc-label"><span>${soc}%</span><span>${targetLbl}</span></div>
    </div>` : '';

  const modeBadge = sc.in_trip_mode
    ? `<span class="badge badge-trip">trip ${sc.trip_soc_pct}%</span>`
    : sc.trip_pending
      ? `<span class="badge badge-trip">trip ${sc.trip_pending_target ?? sc.trip_soc_pct}% pending</span>`
      : `<span class="badge badge-basic">basic ${sc.basic_soc_pct}%</span>`;

  const tripsHtml = (d.trips || []).map(t => `
    <div class="row">
      <span class="label">${t.label} <span style="color:var(--accent)">${t.soc_pct}%</span></span>
      <button class="del-btn" onclick="deleteTrip('${t.id}')">✕</button>
    </div>`).join('');

  document.getElementById('grid').innerHTML = `
    <div class="card">
      <div class="card-title">Epspot</div>
      ${row('Login', dot(ep.ok) + (ep.ok ? ep.user : 'not logged in'))}
      ${row('Balance', ep.balance_sek !== null ? ep.balance_sek + ' kr' : null)}
      ${row('Outlet', outletLabel)}
      ${ep.outlet_state !== 'empty' ? row('Power', ep.outlet_power_w > 0 ? `${(ep.outlet_power_w/1000).toFixed(1)} kW` : '0 kW') : ''}
      ${ep.outlet_amps !== null && ep.evse_offered_amps ? row('Current', `${ep.outlet_amps} A of ${ep.evse_offered_amps} A offered${ep.system_throttled ? ' <span style="color:var(--yellow)">⚠</span>' : ''}`) : ''}
      ${ep.limit_source && ep.limit_source !== 'idle' ? row('Limited by', limitBadge(ep.limit_source)) : ''}
      ${row('Session', ep.active_session_id
          ? ep.active_session_id.slice(0,8) + '… · ' + (ep.active_session_kwh ?? '?') + ' kWh'
          : 'none')}
      ${row('Last poll', ep.last_poll_age)}
    </div>

    <div class="card">
      <div class="card-title">Mercedes EQB</div>
      ${row('Login', dot(mb.ok) + (mb.ok ? 'connected' : 'not connected'))}
      ${row('Charging', mb.charging === null ? null
          : mb.charging ? '<span style="color:var(--green)">yes</span>'
                        : '<span style="color:var(--muted)">no</span>')}
      ${ep.outlet_state !== 'empty' ? row('Charging power', ep.outlet_power_w > 0 ? `${(ep.outlet_power_w/1000).toFixed(1)} kW` : '0 kW') : ''}
      ${row('Distance to charger', distLabel)}
      ${row('GPS', mb.gps)}
      ${row('Updated', mb.updated_age)}
      ${socBar}
    </div>

    <div class="card">
      <div class="card-title">Charging ${modeBadge}</div>
      ${row('Basic SoC', sc.basic_soc_pct + ' %')}
      ${row('Trip SoC', sc.trip_soc_pct + ' %')}
      ${(sc.in_trip_mode || sc.trip_pending) ? row('Trip starts', sc.charge_starts) : ''}
      ${(sc.in_trip_mode || sc.trip_pending) ? row('Ready by', sc.ready_by) : ''}
      ${sc.trip_pending ? row('Hold at', sc.basic_soc_pct + ' % until then') : ''}
      ${row('Done if charging now', sc.est_done_if_charging_now)}
      ${row('Learned rate', sc.charge_rate_kw ? `${sc.charge_rate_kw} kW · ${sc.charge_samples} samples` : `${sc.charge_rate_kw_configured} kW (config)`)}
      ${sc.climate_prep_at ? row('Climate prep at', sc.climate_prep_at) : ''}
      ${d.last_error ? `<div class="error">⚠ ${d.last_error}</div>` : ''}
    </div>

    <div class="card">
      <div class="card-title">Planned Trips</div>
      ${tripsHtml || '<div class="row"><span class="label" style="width:100%;text-align:center">No trips planned</span></div>'}
      <div class="trip-form">
        <input type="datetime-local" id="trip-dt">
        <input type="number" id="trip-soc" min="50" max="100" value="${d.scheduler.trip_soc_pct}" style="flex:0 0 52px;text-align:center">
        <span style="color:var(--muted);font-size:13px;flex:none">%</span>
        <button onclick="addTrip()">Add</button>
      </div>
    </div>
  `;
}

refresh();
setInterval(refresh, 30000);
</script>
</body>
</html>"""


# ── Stats & details page ────────────────────────────────────────────────────

_STATS_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>carcharge · stats</title>
<style>
  :root {
    --bg: #0f1117; --card: #1a1d27; --border: #2d3044;
    --text: #e2e4f0; --muted: #7b7f9e; --green: #4ade80;
    --red: #f87171; --yellow: #fbbf24; --accent: #818cf8;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: system-ui, sans-serif;
         font-size: 14px; padding: 24px; }
  h1 { font-size: 20px; font-weight: 600; color: var(--accent); margin-bottom: 4px; }
  a.navlink { color: var(--accent); text-decoration: none; font-size: 13px; font-weight: 500; margin-left: 14px; }
  a.navlink:hover { text-decoration: underline; }
  .sub { color: var(--muted); font-size: 12px; margin-bottom: 20px; }
  .card { background: var(--card); border: 1px solid var(--border); border-radius: 12px;
          padding: 18px; margin-bottom: 16px; }
  .card-title { font-size: 11px; font-weight: 700; text-transform: uppercase;
                letter-spacing: .08em; color: var(--muted); margin-bottom: 14px; }
  .row { display: flex; justify-content: space-between; padding: 5px 0;
         border-bottom: 1px solid var(--border); }
  .row:last-child { border-bottom: none; }
  .label { color: var(--muted); }
  .val { font-weight: 500; }
  table { border-collapse: collapse; width: 100%; font-size: 13px; }
  th, td { padding: 5px 8px; text-align: right; border-bottom: 1px solid var(--border); }
  th { color: var(--muted); font-weight: 600; font-size: 11px; text-transform: uppercase;
       letter-spacing: .05em; }
  td:first-child, th:first-child { text-align: left; }
  .hm-wrap { overflow-x: auto; }
  .hm { border-collapse: collapse; font-size: 10px; }
  .hm th { padding: 2px 3px; color: var(--muted); font-weight: 500; font-size: 9px; text-transform: none; letter-spacing: 0; border: none; }
  .hm td { padding: 0; border: 1px solid var(--bg); }
  .hm .cell { width: 22px; height: 22px; line-height: 22px; text-align: center;
              color: #0f1117; font-weight: 600; border-radius: 3px; }
  .hm .daylabel { text-align: right; padding-right: 8px; color: var(--muted); font-weight: 600; font-size: 11px; border: none; }
  .legend { display: flex; align-items: center; gap: 6px; margin-top: 12px; font-size: 11px; color: var(--muted); }
  .legend .bar { width: 120px; height: 10px; border-radius: 5px;
                 background: linear-gradient(90deg, hsl(0,55%,42%), hsl(60,55%,42%), hsl(120,55%,42%)); }
  .del-btn { background: none; border: 1px solid var(--border); color: var(--muted);
             border-radius: 6px; cursor: pointer; font-size: 11px; padding: 2px 7px; }
  .del-btn:hover { border-color: var(--red); color: var(--red); }
  .sd-form { display: flex; gap: 8px; margin-top: 14px; flex-wrap: wrap; }
  .sd-form input { background: var(--bg); border: 1px solid var(--border); color: var(--text);
                   border-radius: 6px; padding: 5px 8px; font-size: 13px; }
  .sd-form input[type=text] { flex: 1; min-width: 120px; }
  .sd-form button { background: var(--accent); border: none; color: #fff; border-radius: 6px;
                    padding: 5px 12px; cursor: pointer; font-size: 13px; }
  .sd-form button:hover { opacity: .85; }
  .tag { font-size: 10px; padding: 1px 5px; border-radius: 4px; background: rgba(129,140,248,.2); color: var(--accent); margin-left: 6px; }
  .muted { color: var(--muted); }
</style>
<script>
function api(p) {
  const base = '__BASE__';
  const path = p.replace(/^\\//, '');
  return base ? base + '/' + path : '/' + path;
}
</script>
</head>
<body>
<h1>📊 carcharge stats <a href="__BASE__/" class="navlink">← back</a></h1>
<div class="sub">Learned charging model · auto-refreshes every 60 s</div>

<div class="card">
  <div class="card-title">Model summary</div>
  <div id="summary"><div class="row"><span class="label">Loading…</span></div></div>
</div>

<div class="card">
  <div class="card-title">Charge rate by battery level (taper)</div>
  <div id="bysoc"></div>
</div>

<div class="card">
  <div class="card-title">Charge rate by day &amp; hour (kW)</div>
  <div class="hm-wrap"><div id="heatmap"></div></div>
  <div class="legend"><span>0 kW</span><span class="bar"></span><span>7.4 kW</span>
    <span style="margin-left:12px">empty = no data · hover a cell for detail</span></div>
</div>

<div class="card">
  <div class="card-title">Special days (holidays / eves / manual)</div>
  <div id="special"></div>
  <div class="sd-form">
    <input type="date" id="sd-date">
    <input type="text" id="sd-label" placeholder="label, e.g. School term ends">
    <button onclick="addSpecial()">Add</button>
  </div>
</div>

<script>
const DAY_LABEL = ['Mon','Tue','Wed','Thu','Fri','Sat','Sun','Special'];

function kwColor(kw) {
  if (kw == null) return 'var(--border)';
  const f = Math.max(0, Math.min(1, kw / 7.4));
  return `hsl(${Math.round(f*120)}, 55%, 42%)`;
}

async function loadStats() {
  const d = await (await fetch(api('api/stats'))).json();
  // summary
  const fwd = d.forward_model_active;
  document.getElementById('summary').innerHTML =
      row('Samples collected', d.samples ?? 0)
    + row('Global learned rate', d.global_rate_kw != null ? d.global_rate_kw + ' kW' : '—')
    + row('Calibration (V/A)', d.volts_per_amp != null ? d.volts_per_amp : '—')
    + row('Forward model', fwd
        ? '<span style="color:var(--green)">active (car vs system split)</span>'
        : '<span class="muted">not active yet — using combined estimate</span>');

  // by SoC
  if (d.by_soc) {
    let h = '<table><tr><th>Battery</th><th>Samples</th><th>Mean kW</th><th>± std</th><th>Car accepts</th></tr>';
    for (const b of d.by_soc) {
      h += `<tr><td>${b.range}</td><td>${b.n||0}</td>`
         + `<td>${b.mean_kw ?? '—'}</td>`
         + `<td class="muted">${b.std_kw != null ? '±'+b.std_kw : '—'}</td>`
         + `<td>${b.car_accept_kw != null ? b.car_accept_kw+' kW' : '—'}</td></tr>`;
    }
    document.getElementById('bysoc').innerHTML = h + '</table>';
  }

  // heatmap
  const lut = {}; const present = [];
  for (const e of (d.by_day_hour || [])) {
    lut[e.day_class + '_' + e.hour] = e;
    if (!present.includes(e.day_class)) present.push(e.day_class);
  }
  present.sort((a,b) => a-b);
  if (present.length === 0) {
    document.getElementById('heatmap').innerHTML = '<div class="muted">No data yet.</div>';
  } else {
    let h = '<table class="hm"><tr><th></th>';
    for (let hr = 0; hr < 24; hr++) h += `<th>${hr}</th>`;
    h += '</tr>';
    for (const dc of present) {
      h += `<tr><td class="daylabel">${DAY_LABEL[dc] ?? dc}</td>`;
      for (let hr = 0; hr < 24; hr++) {
        const e = lut[dc + '_' + hr];
        if (e) {
          const tip = `${DAY_LABEL[dc]} ${String(hr).padStart(2,'0')}:00\\n`
            + `mean ${e.mean_kw} kW (n=${e.n})\\n`
            + (e.std_kw!=null?`std ±${e.std_kw} kW · cv ${e.cv}\\n`:'')
            + (e.system_offered_amps!=null?`system offered ${e.system_offered_amps} A → ${e.system_avail_kw} kW\\n`:'')
            + `safety rate ${e.safety_kw} kW`;
          h += `<td><div class="cell" style="background:${kwColor(e.mean_kw)}" title="${tip}">${e.mean_kw.toFixed(1)}</div></td>`;
        } else {
          h += `<td><div class="cell" style="background:var(--border)"></div></td>`;
        }
      }
      h += '</tr>';
    }
    document.getElementById('heatmap').innerHTML = h + '</table>';
  }
}

async function loadSpecial() {
  const d = await (await fetch(api('api/special-days'))).json();
  const up = d.upcoming || [];
  let h = up.length ? '' : '<div class="row"><span class="muted" style="width:100%;text-align:center">None in the next 90 days</span></div>';
  for (const s of up) {
    h += `<div class="row"><span class="label">${s.date} · ${s.label}`
       + (s.manual ? '<span class="tag">manual</span>' : '') + '</span>'
       + (s.manual ? `<button class="del-btn" onclick="deleteSpecial('${s.date}')">✕</button>` : '<span class="muted">auto</span>')
       + '</div>';
  }
  document.getElementById('special').innerHTML = h;
}

function row(label, val) {
  return `<div class="row"><span class="label">${label}</span><span class="val">${val ?? '—'}</span></div>`;
}

async function addSpecial() {
  const date = document.getElementById('sd-date').value;
  const label = document.getElementById('sd-label').value.trim() || 'special';
  if (!date) return;
  const r = await fetch(api('api/special-days'), {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({date, label}),
  });
  if (r.ok) { document.getElementById('sd-date').value=''; document.getElementById('sd-label').value=''; loadSpecial(); }
}
async function deleteSpecial(date) {
  await fetch(api('api/special-days/' + date), {method: 'DELETE'});
  loadSpecial();
}

function refreshAll() { loadStats().catch(()=>{}); loadSpecial().catch(()=>{}); }
refreshAll();
setInterval(refreshAll, 60000);
</script>
</body>
</html>"""
