"""
Calendly booking webhook for Martina (Retell custom functions).

Two endpoints Retell calls mid-conversation:
  POST /availability  -> { timezone } -> next open slots in the lead's local time
  POST /book          -> { name, email, timezone, start_time } -> books on Calendly

Retell sends function args either at the top level or under "args"; we handle both.

Env vars (set in Render):
  CALENDLY_TOKEN     Michael's Calendly Personal Access Token  (required)
  EVENT_TYPE_URI     the veteran-benefit event type URI         (required)
  LOCATION_KIND      meeting location kind, default zoom_conference
  SHARED_SECRET      optional: require header X-Secret to match (recommended)
"""
import os, datetime as dt
from zoneinfo import ZoneInfo
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)
CAL = "https://api.calendly.com"
TOKEN = os.environ.get("CALENDLY_TOKEN", "")
EVENT_TYPE_URI = os.environ.get("EVENT_TYPE_URI", "")
LOCATION_KIND = os.environ.get("LOCATION_KIND", "zoom_conference")
SHARED_SECRET = os.environ.get("SHARED_SECRET", "")
MAX_SLOTS = int(os.environ.get("MAX_SLOTS", "3"))


def _h():
    # Explicit User-Agent: Calendly's Cloudflare WAF blocks some default library UAs.
    return {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json",
            "User-Agent": "MartinaCalendly/1.0"}


def _args():
    body = request.get_json(silent=True) or {}
    a = body.get("args")
    return a if isinstance(a, dict) else body


def _auth_ok():
    return not SHARED_SECRET or request.headers.get("X-Secret") == SHARED_SECRET


@app.get("/health")
def health():
    return jsonify(ok=True, event_type_set=bool(EVENT_TYPE_URI), token_set=bool(TOKEN))


@app.get("/event_types")
def event_types():
    """Setup helper: list event types so you can find the right EVENT_TYPE_URI."""
    me = requests.get(f"{CAL}/users/me", headers=_h(), timeout=20).json()
    org = me["resource"]["current_organization"]
    user = me["resource"]["uri"]
    r = requests.get(f"{CAL}/event_types", headers=_h(),
                     params={"user": user, "organization": org, "active": "true"}, timeout=20).json()
    out = [{"name": e["name"], "duration": e.get("duration"), "uri": e["uri"],
            "scheduling_url": e.get("scheduling_url")} for e in r.get("collection", [])]
    return jsonify(out)


@app.post("/availability")
def availability():
    if not _auth_ok():
        return jsonify(error="unauthorized"), 401
    tz = (_args().get("timezone") or "America/Chicago").strip()
    try:
        zone = ZoneInfo(tz)
    except Exception:
        zone, tz = ZoneInfo("America/Chicago"), "America/Chicago"
    now = dt.datetime.now(dt.timezone.utc)
    start = (now + dt.timedelta(minutes=60))
    end = (now + dt.timedelta(days=7) - dt.timedelta(minutes=1))
    fmt = "%Y-%m-%dT%H:%M:%S.000000Z"
    r = requests.get(f"{CAL}/event_type_available_times", headers=_h(),
                     params={"event_type": EVENT_TYPE_URI,
                             "start_time": start.strftime(fmt),
                             "end_time": end.strftime(fmt)}, timeout=20)
    if r.status_code != 200:
        return jsonify(error="availability_failed", detail=r.text[:300]), 200
    times = r.json().get("collection", [])
    slots = []
    for t in times[:MAX_SLOTS]:
        utc = dt.datetime.fromisoformat(t["start_time"].replace("Z", "+00:00"))
        local = utc.astimezone(zone)
        slots.append({"when": local.strftime("%A at %-I:%M %p"),
                      "start_time": t["start_time"]})
    if not slots:
        return jsonify(slots=[], message="No openings in the next 7 days."), 200
    return jsonify(slots=slots, timezone=tz), 200


@app.post("/book")
def book():
    if not _auth_ok():
        return jsonify(error="unauthorized"), 401
    a = _args()
    name = (a.get("name") or "").strip()
    email = (a.get("email") or "").strip()
    start_time = (a.get("start_time") or "").strip()
    tz = (a.get("timezone") or "America/Chicago").strip()
    if not (name and email and start_time):
        return jsonify(booked=False, error="missing name, email, or start_time"), 200
    payload = {"event_type": EVENT_TYPE_URI, "start_time": start_time,
               "invitee": {"name": name, "email": email, "timezone": tz}}
    if LOCATION_KIND:
        payload["location"] = {"kind": LOCATION_KIND}
    r = requests.post(f"{CAL}/invitees", headers=_h(), json=payload, timeout=25)
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
