# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

An outbound phone call campaign tool. It takes a list of phone numbers, calls them one by one using Retell AI as the voice agent, and displays live results + post-call analysis on a web dashboard.

## Running locally

```bash
pip install -r requirements.txt
python app.py          # runs on port 5000
```

Requires a `.env` file with:
```
RETELL_API_KEY=...
RETELL_AGENT_ID=...
TWILIO_FROM_NUMBER=...
```

For Retell AI to send webhooks to localhost, use ngrok:
```bash
ngrok http 5000
# paste https://<id>.ngrok.io/webhook into Retell AI dashboard
```

## Deployment

Hosted on Railway. Push to `main` branch → auto-deploys.
- Production URL: `https://web-production-8c973.up.railway.app`
- Webhook URL (paste into Retell AI): `https://web-production-8c973.up.railway.app/webhook`
- Environment variables are set in Railway service Variables tab (not Shared Variables)

## Architecture

Single file backend (`app.py`) + single template (`templates/index.html`).

**Campaign state** is held in a single in-memory dict (`campaign`) protected by `campaign_lock` (threading.Lock). This resets on every server restart — there is no database.

**Call flow:**
1. `POST /start` → initialises `campaign["calls"]` list, spawns `_trigger_next_call` thread
2. `_trigger_next_call` → calls `make_call()` → hits Retell AI `POST /v2/create-phone-call`
3. After call ends, Retell AI POSTs to `/webhook` with `event=call_ended` → advances `current_index` → spawns next `_trigger_next_call`
4. Retell AI POSTs a second webhook with `event=call_analyzed` → updates summary/sentiment on the call record
5. Frontend polls `GET /status` every 2s and rebuilds the call list

**Loop prevention:** `_trigger_next_call` only advances via webhook (`event=call_ended`). It will not auto-retry failed calls. The `/retry/<idx>` endpoint is the only way to re-call a number — it requires explicit user action.

**Key routes:**
- `POST /start` — begin campaign (resets all state)
- `POST /stop` — sets `running=False`, halts between-call progression
- `POST /reset` — clears all state
- `POST /retry/<idx>` — manually re-call one number by its list index
- `POST /webhook` — receives Retell AI events
- `GET /status` — returns full campaign state for frontend polling

## Retell AI webhook events

Two distinct events arrive per call:
- `call_ended` — has `call_status`, `duration_ms`, `disconnection_reason`, `recording_url`
- `call_analyzed` — has `call_analysis.call_summary`, `user_sentiment`, `in_voicemail`, `call_successful`

Both are matched to the correct call record via `call_id`.
