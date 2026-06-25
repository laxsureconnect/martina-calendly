"""
Calendly booking webhook for Koagent agents (Retell custom functions) — MULTI-PRODUCER.

Two endpoints Retell calls mid-conversation:
  POST /availability  -> { timezone, producer } -> next open slots in the lead's local time
  POST /book          -> { name, email, timezone, start_time, producer } -> books on Calendly

Each producer has their OWN Calendly account (own token + event). The agent passes a
"producer" key (set per-agent via the {{producer}} dynamic variable). If no producer is
sent, we fall back to the default (Michael) using the legacy CALENDLY_TOKEN/EVENT_TYPE_URI
env vars — so Michael's existing agent keeps working with zero changes.

Env vars (set in Render):
  CALENDLY_TOKEN     Michael's Calendly token   (legacy/default producer)
  EVENT_TYPE_URI     Michael's event type URI   (legacy/default producer)
  PRODUCERS          JSON map of producer_key -> {token, event_type_uri OR scheduling_slug}
                     e.g. {"greg":{"token":"...","scheduling_slug":"appointment-greg-anderson-ao-globe-life"}}
                     If only scheduling_slug is given, the event_type_uri is resolved from
                     the token on first use and cached.
  LOCATION_KIND      meeting location kind, default zoom_conference
  SHARED_SECRET      optional: require header X-Secret to match (recommended)
"""
import os, json, datetime as dt
from zoneinfo import ZoneInfo
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)
CAL = "https://api.calendly.com"
LOCATION_KIND = os.environ.get("LOCATION_KIND", "zoom_conference")
SHARED_SECRET = os.environ.get("SHARED_SECRET", "")
MAX_SLOTS = int(os.environ.get("MAX_SLOTS", "18"))
SLOTS_PER_DAY = int(os.environ.get("SLOTS_PER_DAY", "3"))
DAYS_AHEAD = int(os.environ.get("DAYS_AHEAD", "7"))

# Default (legacy) producer = Michael, from the original env vars.
DEFAULT_PRODUCER = "michael"
_BASE = {
    "michael": {"token": os.environ.get("CALENDLY_TOKEN", ""),
                "event_type_uri": os.environ.get("EVENT_TYPE_URI", "")},
}
try:
    _BASE.update(json.loads(os.environ.get("PRODUCERS", "{}")))
except Exception:
    pass

# in-memory cache of resolved event_type_uris (keyed by producer)
_URI_CACHE = {}


def _h(token):
    # Explicit User-Agent: Calendly's Cloudflare WAF blocks some default library UAs.
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json",
            "User-Agent": "KoagentCalendly/1.0"}


def _resolve_event_uri(token, slug):
    """Find a producer's event_type_uri from their scheduling-link slug."""
    me = requests.get(f"{CAL}/users/me", headers=_h(token), timeout=20).json()["resource"]
    r = requests.get(f"{CAL}/event_types", headers=_h(token),
                     params={"user": me["uri"], "organization": me["current_organization"],
                             "active": "true"}, timeout=20).json()
    for e in r.get("collection", []):
        if (e.get("slug") or "") == slug or slug in (e.get("scheduling_url") or ""):
            return e["uri"]
    # fall back to the first active event type if slug didn't match
    coll = r.get("collection", [])
    return coll[0]["uri"] if coll else ""


def _creds(producer):
    """Return (token, event_type_uri) for a producer, resolving + caching the URI if needed."""
    key = (producer or DEFAULT_PRODUCER).strip().lower()
    cfg = _BASE.get(key) or _BASE.get(DEFAULT_PRODUCER, {})
    token = cfg.get("token", "")
    uri = cfg.get("event_type_uri", "") or _URI_CACHE.get(key, "")
    if not uri and token and cfg.get("scheduling_slug"):
        uri = _resolve_event_uri(token, cfg["scheduling_slug"])
        _URI_CACHE[key] = uri
    return token, uri


def _args():
    body = request.get_json(silent=True) or {}
    a = body.get("args")
    return a if isinstance(a, dict) else body


def _auth_ok():
    return not SHARED_SECRET or request.headers.get("X-Secret") == SHARED_SECRET


@app.get("/health")
def health():
    return jsonify(ok=True, producers=sorted(_BASE.keys()))


@app.get("/event_types")
def event_types():
    """Setup helper: list event types for a producer (?producer=greg)."""
    token, _ = _creds(request.args.get("producer"))
    me = requests.get(f"{CAL}/users/me", headers=_h(token), timeout=20).json()
    org = me["resource"]["current_organization"]; user = me["resource"]["uri"]
    r = requests.get(f"{CAL}/event_types", headers=_h(token),
                     params={"user": user, "organization": org, "active": "true"}, timeout=20).json()
    return jsonify([{"name": e["name"], "duration": e.get("duration"), "uri": e["uri"],
                     "scheduling_url": e.get("scheduling_url")} for e in r.get("collection", [])])


@app.post("/availability")
def availability():
    if not _auth_ok():
        return jsonify(error="unauthorized"), 401
    a = _args()
    token, event_uri = _creds(a.get("producer"))
    if not (token and event_uri):
        return jsonify(slots=[], message="Booking not configured for this producer."), 200
    tz = (a.get("timezone") or "America/Chicago").strip()
    try:
        zone = ZoneInfo(tz)
    except Exception:
        zone, tz = ZoneInfo("America/Chicago"), "America/Chicago"
    now = dt.datetime.now(dt.timezone.utc)
    start = (now + dt.timedelta(minutes=60))
    end = (now + dt.timedelta(days=DAYS_AHEAD) - dt.timedelta(minutes=1))
    fmt = "%Y-%m-%dT%H:%M:%S.000000Z"
    r = requests.get(f"{CAL}/event_type_available_times", headers=_h(token),
                     params={"event_type": event_uri,
                             "start_time": start.strftime(fmt),
                             "end_time": end.strftime(fmt)}, timeout=20)
    if r.status_code != 200:
        return jsonify(error="availability_failed", detail=r.text[:300]), 200
    times = r.json().get("collection", [])
    by_day = {}
    for t in times:
        utc = dt.datetime.fromisoformat(t["start_time"].replace("Z", "+00:00"))
        local = utc.astimezone(zone)
        by_day.setdefault(local.date(), []).append((local, t["start_time"]))
    slots = []
    for day in sorted(by_day):
        daily = sorted(by_day[day]); n = len(daily)
        if n <= SLOTS_PER_DAY:
            picks = daily
        else:
            idxs = sorted({round(i * (n - 1) / (SLOTS_PER_DAY - 1)) for i in range(SLOTS_PER_DAY)})
            picks = [daily[i] for i in idxs]
        for local, start_time in picks:
            slots.append({"when": local.strftime("%A at %-I:%M %p"), "start_time": start_time})
        if len(slots) >= MAX_SLOTS:
            break
    slots = slots[:MAX_SLOTS]
    if not slots:
        return jsonify(slots=[], message=f"No openings in the next {DAYS_AHEAD} days."), 200
    return jsonify(slots=slots, timezone=tz), 200


@app.post("/book")
def book():
    if not _auth_ok():
        return jsonify(error="unauthorized"), 401
    a = _args()
    token, event_uri = _creds(a.get("producer"))
    name = (a.get("name") or "").strip()
    email = (a.get("email") or "").strip()
    start_time = (a.get("start_time") or "").strip()
    tz = (a.get("timezone") or "America/Chicago").strip()
    if not (token and event_uri):
        return jsonify(booked=False, error="producer_not_configured"), 200
    if not (name and email and start_time):
        return jsonify(booked=False, error="missing name, email, or start_time"), 200
    payload = {"event_type": event_uri, "start_time": start_time,
               "invitee": {"name": name, "email": email, "timezone": tz}}
    if LOCATION_KIND:
        payload["location"] = {"kind": LOCATION_KIND}
    r = requests.post(f"{CAL}/invitees", headers=_h(token), json=payload, timeout=25)
    if r.status_code in (200, 201):
        d = r.json().get("resource", {})
        return jsonify(booked=True, reschedule_url=d.get("reschedule_url"),
                       cancel_url=d.get("cancel_url")), 200
    if r.status_code in (404, 409):
        return jsonify(booked=False, error="slot_taken",
                       message="That time was just taken — offer another slot."), 200
    return jsonify(booked=False, error=f"calendly_{r.status_code}", detail=r.text[:300]), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
