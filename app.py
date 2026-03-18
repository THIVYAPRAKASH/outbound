import os
import csv
import io
import time
import threading
import requests
from flask import Flask, request, jsonify, render_template, Response
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────
RETELL_API_KEY     = os.getenv("RETELL_API_KEY", "")
RETELL_AGENT_ID    = os.getenv("RETELL_AGENT_ID", "")
TWILIO_FROM_NUMBER = os.getenv("TWILIO_FROM_NUMBER", "")

# ── Campaign state ─────────────────────────────────────────────────────────────
campaign = {
    "numbers":       [],
    "current_index": 0,
    "calls":         [],
    "running":       False,
    "paused":        False,
    "delay":         2,
}
campaign_lock = threading.Lock()

# Deduplication: track call_ids whose call_ended has already been handled
_ended_call_ids = set()


# ── Helpers ────────────────────────────────────────────────────────────────────

def _fmt_duration(ms):
    if not ms:
        return None
    s = ms // 1000
    m, s = divmod(s, 60)
    return f"{m}m {s}s" if m else f"{s}s"


def make_call(to_number):
    url = "https://api.retellai.com/v2/create-phone-call"
    headers = {
        "Authorization": f"Bearer {RETELL_API_KEY}",
        "Content-Type":  "application/json",
    }
    payload = {
        "from_number": TWILIO_FROM_NUMBER,
        "to_number":   to_number,
        "agent_id":    RETELL_AGENT_ID,
    }
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=15)
        resp.raise_for_status()
        return resp.json().get("call_id", "unknown"), None
    except requests.exceptions.HTTPError as e:
        return None, f"HTTP {e.response.status_code}: {e.response.text}"
    except Exception as e:
        return None, str(e)


def _trigger_next_call():
    with campaign_lock:
        if not campaign["running"] or campaign["paused"]:
            return
        idx = campaign["current_index"]
        if idx >= len(campaign["numbers"]):
            campaign["running"] = False
            return
        # Guard: don't call a number that's already active
        if campaign["calls"][idx]["status"] in ("calling", "in-progress"):
            return
        to_number = campaign["numbers"][idx]
        campaign["calls"][idx]["status"] = "calling"
        delay = campaign["delay"]
        first = idx == 0

    if not first:
        time.sleep(delay)

    # Re-check after sleep — may have been stopped or paused
    with campaign_lock:
        if not campaign["running"] or campaign["paused"]:
            campaign["calls"][idx]["status"] = "pending"
            return

    call_id, error = make_call(to_number)

    with campaign_lock:
        if error:
            campaign["calls"][idx]["status"]  = "error"
            campaign["calls"][idx]["summary"] = error
            campaign["current_index"] += 1
            should_continue = campaign["running"] and not campaign["paused"]
        else:
            campaign["calls"][idx]["call_id"] = call_id
            campaign["calls"][idx]["status"]  = "in-progress"
            should_continue = False  # wait for webhook

    if error and should_continue:
        threading.Thread(target=_trigger_next_call, daemon=True).start()


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/start", methods=["POST"])
def start_campaign():
    data    = request.get_json()
    numbers = [n.strip() for n in data.get("numbers", []) if n.strip()]
    delay   = int(data.get("delay", 2))

    if not numbers:
        return jsonify({"error": "No numbers provided"}), 400

    with campaign_lock:
        _ended_call_ids.clear()
        campaign["numbers"]       = numbers
        campaign["current_index"] = 0
        campaign["delay"]         = delay
        campaign["paused"]        = False
        campaign["calls"]         = [
            {
                "number":               n,
                "status":               "pending",
                "call_id":              None,
                "duration":             None,
                "summary":              None,
                "sentiment":            None,
                "voicemail":            False,
                "successful":           None,
                "disconnection_reason": None,
                "recording_url":        None,
            }
            for n in numbers
        ]
        campaign["running"] = True

    threading.Thread(target=_trigger_next_call, daemon=True).start()
    return jsonify({"message": f"Campaign started with {len(numbers)} number(s)"})


@app.route("/stop", methods=["POST"])
def stop_campaign():
    with campaign_lock:
        campaign["running"] = False
        campaign["paused"]  = False
    return jsonify({"message": "Campaign stopped"})


@app.route("/pause", methods=["POST"])
def pause_campaign():
    with campaign_lock:
        campaign["paused"] = True
    return jsonify({"message": "Campaign paused"})


@app.route("/resume", methods=["POST"])
def resume_campaign():
    with campaign_lock:
        if not campaign["calls"]:
            return jsonify({"error": "No campaign to resume"}), 400
        campaign["paused"]  = False
        campaign["running"] = True
    threading.Thread(target=_trigger_next_call, daemon=True).start()
    return jsonify({"message": "Campaign resumed"})


@app.route("/reset", methods=["POST"])
def reset_campaign():
    with campaign_lock:
        _ended_call_ids.clear()
        campaign["numbers"]       = []
        campaign["current_index"] = 0
        campaign["calls"]         = []
        campaign["running"]       = False
        campaign["paused"]        = False
    return jsonify({"message": "Reset complete"})


@app.route("/retry/<int:idx>", methods=["POST"])
def retry_call(idx):
    """Manually retry a single call. Only works when not already active."""
    with campaign_lock:
        if idx < 0 or idx >= len(campaign["calls"]):
            return jsonify({"error": "Invalid index"}), 400
        if campaign["calls"][idx]["status"] in ("calling", "in-progress"):
            return jsonify({"error": "Call already in progress"}), 400
        # Remove from dedup set so webhook can be processed again
        old_cid = campaign["calls"][idx].get("call_id")
        if old_cid in _ended_call_ids:
            _ended_call_ids.discard(old_cid)
        # Reset record
        campaign["calls"][idx].update({
            "status":               "calling",
            "call_id":              None,
            "duration":             None,
            "summary":              None,
            "sentiment":            None,
            "voicemail":            False,
            "successful":           None,
            "disconnection_reason": None,
            "recording_url":        None,
        })
        to_number = campaign["calls"][idx]["number"]

    call_id, error = make_call(to_number)

    with campaign_lock:
        if error:
            campaign["calls"][idx]["status"]  = "error"
            campaign["calls"][idx]["summary"] = error
        else:
            campaign["calls"][idx]["call_id"] = call_id
            campaign["calls"][idx]["status"]  = "in-progress"

    return jsonify({"error": error} if error else {"call_id": call_id})


@app.route("/webhook", methods=["POST"])
def webhook():
    data    = request.get_json(silent=True) or {}
    event   = data.get("event", "")
    call_id = data.get("call_id")

    # ── Post-call analysis ────────────────────────────────────────────────────
    if event == "call_analyzed":
        analysis = data.get("call_analysis", {}) or {}
        with campaign_lock:
            for i, call in enumerate(campaign["calls"]):
                if call.get("call_id") == call_id:
                    campaign["calls"][i]["summary"]    = analysis.get("call_summary") or ""
                    campaign["calls"][i]["sentiment"]  = analysis.get("user_sentiment", "Unknown")
                    campaign["calls"][i]["voicemail"]  = bool(analysis.get("in_voicemail", False))
                    campaign["calls"][i]["successful"] = bool(analysis.get("call_successful", False))
                    break
        return jsonify({"received": True})

    # ── Call ended — deduplicated, only fires once per call_id ────────────────
    if event == "call_ended":
        with campaign_lock:
            if call_id in _ended_call_ids:
                return jsonify({"received": True})   # duplicate, ignore
            _ended_call_ids.add(call_id)

        start  = data.get("start_timestamp", 0)
        end    = data.get("end_timestamp", 0)
        dur_ms = data.get("duration_ms") or (end - start if start and end else 0)
        call_status = data.get("call_status", "ended")
        disconn     = data.get("disconnection_reason", "")
        rec_url     = data.get("recording_url", "")

        trigger_next = False
        with campaign_lock:
            for i, call in enumerate(campaign["calls"]):
                if call.get("call_id") == call_id:
                    # Only update if not already finalised
                    if campaign["calls"][i]["status"] not in ("ended", "error"):
                        campaign["calls"][i]["status"]               = call_status or "ended"
                        campaign["calls"][i]["duration"]             = _fmt_duration(dur_ms)
                        campaign["calls"][i]["disconnection_reason"] = disconn
                        campaign["calls"][i]["recording_url"]        = rec_url
                        campaign["current_index"]                    = i + 1
                        trigger_next = campaign["running"] and not campaign["paused"]
                    break

        if trigger_next:
            threading.Thread(target=_trigger_next_call, daemon=True).start()

    return jsonify({"received": True})


@app.route("/status")
def status():
    with campaign_lock:
        calls     = campaign["calls"]
        total     = len(calls)
        calling   = sum(1 for c in calls if c["status"] in ("calling", "in-progress"))
        done      = sum(1 for c in calls if c["status"] == "ended")
        failed    = sum(1 for c in calls if c["status"] == "error")
        voicemail = sum(1 for c in calls if c.get("voicemail"))
        pending   = sum(1 for c in calls if c["status"] == "pending")
        calls_out = [dict(c, index=i) for i, c in enumerate(calls)]
        return jsonify({
            "running": campaign["running"],
            "paused":  campaign["paused"],
            "calls":   calls_out,
            "stats": {
                "total":     total,
                "calling":   calling,
                "done":      done,
                "failed":    failed,
                "voicemail": voicemail,
                "pending":   pending,
            },
        })


@app.route("/export")
def export_csv():
    """Download all call results as CSV."""
    with campaign_lock:
        calls = list(campaign["calls"])

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Number", "Status", "Duration", "Sentiment",
        "Voicemail", "Successful", "Disconnect Reason", "Summary", "Recording URL"
    ])
    for c in calls:
        writer.writerow([
            c.get("number", ""),
            c.get("status", ""),
            c.get("duration", ""),
            c.get("sentiment", ""),
            "Yes" if c.get("voicemail") else "No",
            "Yes" if c.get("successful") else "No",
            c.get("disconnection_reason", ""),
            c.get("summary", ""),
            c.get("recording_url", ""),
        ])

    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=campaign_results.csv"}
    )


# ── Run ────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
