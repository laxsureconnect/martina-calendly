# Martina ↔ Calendly booking service

Small webhook that lets Martina check Michael's real Calendly availability and book directly during the call.

## What it does
- `POST /availability` → returns the next open slots in the veteran's local timezone
- `POST /book` → books the chosen slot on Calendly (Create Event Invitee)
- `GET /event_types` → setup helper to find the event type URI
- `GET /health` → status check

## Deploy to Render (one time)

1. Put this `calendly_service/` folder in a GitHub repo (or push the whole project).
2. Render → New → Web Service → connect the repo, root directory `calendly_service`.
   (Render auto-detects `render.yaml`. Otherwise: Build `pip install -r requirements.txt`, Start `gunicorn app:app --timeout 30`.)
3. Set environment variables:
   - `CALENDLY_TOKEN` = Michael's Personal Access Token (calendly.com/integrations/api_webhooks)
   - `EVENT_TYPE_URI` = the veteran-benefit event type URI (see next step)
   - `LOCATION_KIND` = `zoom_conference` (leave as-is if the meeting is Zoom; delete if the event type has no location)
   - `SHARED_SECRET` = any random string (Retell will send it as header `X-Secret`)
4. Deploy. You'll get a URL like `https://martina-calendly.onrender.com`.

## Find the EVENT_TYPE_URI
After the token is set, open `https://<your-url>/event_types` in a browser. Copy the `uri` of the veteran-benefit meeting and set it as `EVENT_TYPE_URI`, then redeploy.

## Wire into Retell (two custom functions)

In the Martina agent → Functions, add two custom functions pointing at this service.

**get_availability** → `POST https://<your-url>/availability`, header `X-Secret: <SHARED_SECRET>`
Parameters:
```json
{ "type":"object","properties":{
  "timezone":{"type":"string","description":"IANA timezone of the veteran, e.g. America/Chicago. Use {{lead_timezone}}."}
}, "required":["timezone"] }
```

**book_slot** → `POST https://<your-url>/book`, header `X-Secret: <SHARED_SECRET>`
Parameters:
```json
{ "type":"object","properties":{
  "name":{"type":"string","description":"Veteran full name, {{lead_name}}"},
  "email":{"type":"string","description":"Veteran email, confirmed on the call"},
  "timezone":{"type":"string","description":"IANA timezone, {{lead_timezone}}"},
  "start_time":{"type":"string","description":"Exact start_time value returned by get_availability for the slot the veteran chose"}
}, "required":["name","email","timezone","start_time"] }
```

## Prompt change (booking step)
Replace the hardcoded two-slot logic with:
> "Let me check Michael's calendar, one moment." → call **get_availability** with {{lead_timezone}} → offer the returned `when` values → once they pick, confirm their email → call **book_slot** with the chosen `start_time`. If `book_slot` returns `slot_taken`, apologize and offer the remaining slots. On success, confirm: "You're all set for [when] — Michael will email you the invite."

## Local test
```
CALENDLY_TOKEN=... EVENT_TYPE_URI=... python app.py
curl -s localhost:8000/health
curl -s -X POST localhost:8000/availability -H 'content-type: application/json' -d '{"timezone":"America/Chicago"}'
```
