import os
import threading
import requests
from flask import Flask, request, jsonify, render_template
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

# ── Config (loaded from .env) ──────────────────────────────────────────────────
RETELL_API_KEY    = os.getenv("RETELL_API_KEY", "")
RETELL_AGENT_ID   = os.getenv("RETELL_AGENT_ID", "")
TWILIO_FROM_NUMBER = os.getenv("TWILIO_FROM_NUMBER", "")

# ── In-memory campaign state ───────────────────────────────────────────────────
campaign = {
    "numbers": [],       # full list of numbers to call
    "current_index": 0,  # which number we're on
    "calls": [],         # list of call result dicts
    "running": False,
}
campaign_lock = threading.Lock()


def make_call(to_number):
    """Fire an outbound call via Retell AI API."""
    url = "https://api.retellai.com/v2/create-phone-call"
    headers = {
        "Authorization": f"Bearer {RETELL_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "from_number": TWILIO_FROM_NUMBER,
        "to_number": to_number,
        "agent_id": RETELL_AGENT_ID,
    }
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        call_id = data.get("call_id", "unknown")
        return call_id, None
    except requests.exceptions.HTTPError as e:
        return None, f"HTTP {e.response.status_code}: {e.response.text}"
    except Exception as e:
        return None, str(e)


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/start", methods=["POST"])
def start_campaign():
    """Receive list of numbers from the dashboard and kick off the first call."""
    data = request.get_json()
    numbers = [n.strip() for n in data.get("numbers", []) if n.strip()]

    if not numbers:
        return jsonify({"error": "No numbers provided"}), 400

    with campaign_lock:
        campaign["numbers"] = numbers
        campaign["current_index"] = 0
        campaign["calls"] = [
            {"number": n, "status": "pending", "call_id": None, "summary": None}
            for n in numbers
        ]
        campaign["running"] = True

    # Trigger first call in a background thread so we return immediately
    threading.Thread(target=_trigger_next_call, daemon=True).start()
    return jsonify({"message": f"Campaign started with {len(numbers)} number(s)"})


def _trigger_next_call():
    """Called in a background thread. Places the next queued call."""
    with campaign_lock:
        idx = campaign["current_index"]
        if idx >= len(campaign["numbers"]):
            campaign["running"] = False
            return
        if campaign["calls"][idx]["status"] in ("calling", "in-progress"):
            return
        to_number = campaign["numbers"][idx]
        campaign["calls"][idx]["status"] = "calling"

    call_id, error = make_call(to_number)

    with campaign_lock:
        if error:
            campaign["calls"][idx]["status"] = "error"
            campaign["calls"][idx]["summary"] = error
            # Move to next number even if this one errored
            campaign["current_index"] += 1
            # Trigger next call
            threading.Thread(target=_trigger_next_call, daemon=True).start()
        else:
            campaign["calls"][idx]["call_id"] = call_id
            campaign["calls"][idx]["status"] = "in-progress"
            # We wait here — next call is triggered by the webhook


TERMINAL_STATUSES = {"ended", "error", "failed"}


@app.route("/webhook", methods=["POST"])
def webhook():
    """Retell AI posts here after each call ends."""
    data = request.get_json(silent=True) or {}

    call_id     = data.get("call_id")
    call_status = data.get("call_status", "")
    analysis    = data.get("call_analysis", {})
    transcript  = data.get("transcript", "")
    summary     = analysis.get("summary") or (transcript[:300] if transcript else "No summary available")

    trigger_next = False
    with campaign_lock:
        for i, call in enumerate(campaign["calls"]):
            if call.get("call_id") == call_id:
                campaign["calls"][i]["status"] = call_status or "ended"
                if call_status in TERMINAL_STATUSES:
                    campaign["calls"][i]["summary"] = summary
                    campaign["current_index"] = i + 1
                    trigger_next = True
                break

    if trigger_next:
        threading.Thread(target=_trigger_next_call, daemon=True).start()

    return jsonify({"received": True})


@app.route("/status")
def status():
    """Dashboard polls this every few seconds to get live updates."""
    with campaign_lock:
        return jsonify({
            "running": campaign["running"],
            "calls": campaign["calls"],
        })


# ── Run ────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
