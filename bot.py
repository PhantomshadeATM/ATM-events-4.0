import requests
import time
import json
import os
import sys
import io
import logging
import threading
from datetime import datetime, timezone
from requests.adapters import HTTPAdapter, Retry
from flask import Flask, jsonify
from collections import deque

    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

API_URL = "https://api.truckersmp.com/v2/vtc/49940/events/attending"
WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]
ERROR_WEBHOOK_URL = os.environ.get("DISCORD_ERROR_WEBHOOK_URL")
HEARTBEAT_WEBHOOK_URL = os.environ.get("DISCORD_HEARTBEAT_WEBHOOK_URL")
DAILY_SUMMARY_WEBHOOK_URL = os.environ.get("DISCORD_DAILY_SUMMARY_WEBHOOK_URL")

DB_FILE = "events_db.json"
START_TIME = time.time()

API_LATENCIES = []
LAST_API_LATENCY = None

EVENTS_ADDED_TODAY = 0
EVENTS_UPDATED_TODAY = 0
EVENTS_REMOVED_TODAY = 0
TOTAL_ACTIVE_EVENTS = 0

API_CHECKS_TODAY = 0
API_SUCCESSES_TODAY = 0

HEARTBEATS_SENT_TODAY = 0
ERRORS_REPORTED_TODAY = 0
RESTARTS_TODAY = 0

LAST_SUCCESSFUL_SYNC = None
LAST_SUMMARY_DATE = None

ATM_COLOR = 0x770202
ATM_LOGO_URL = "https://cdn.imgpile.com/f/0PFveX5_xl.png"
ATM_BANNER_URL = "https://cdn.imgpile.com/f/13NFeJc_xl.png"

# ---------- Logging setup ----------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("ATM-Bot")

LOG_BUFFER = deque(maxlen=200)


class LogCaptureHandler(logging.Handler):
    def emit(self, record):
        LOG_BUFFER.append(self.format(record))


capture_handler = LogCaptureHandler()
capture_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(capture_handler)

# ---------- HTTP session ----------

session = requests.Session()
retries = Retry(
    total=5,
    backoff_factor=2,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["GET", "POST"]
)
session.mount("https://", HTTPAdapter(max_retries=retries))


def ensure_database_exists():
    if not os.path.exists(DB_FILE):
        with open(DB_FILE, "w", encoding="utf-8") as f:
            json.dump({}, f, indent=2)
        logger.info("Created empty events_db.json")


def should_restart():
    return (time.time() - START_TIME) >= 86400


def load_db():
    try:
        with open(DB_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Failed to load DB: {e}")
        return {}


def save_db(data):
    try:
        with open(DB_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        logger.info("Database saved")
    except Exception as e:
        logger.error(f"Failed to save DB: {e}")


def report_error(title, message, details=None):
    global ERRORS_REPORTED_TODAY
    ERRORS_REPORTED_TODAY += 1

    logger.error(f"{title}: {message}")

    if not ERROR_WEBHOOK_URL:
        if details:
            logger.error(details)
        return

    embed = {
        "username": "TruckersMP Events Bot — Error Reporter",
        "embeds": [
            {
                "title": f"⚠️ {title}",
                "color": 0xE74C3C,
                "description": message,
                "fields": [],
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        ],
    }

    if details:
        embed["embeds"][0]["fields"].append({
            "name": "Details",
            "value": f"```{details[:1900]}```",
            "inline": False
        })

    try:
        session.post(ERROR_WEBHOOK_URL, json=embed, timeout=15)
    except Exception as e:
        logger.error(f"Failed to send error report: {e}")


def record_latency(latency):
    global LAST_API_LATENCY, API_LATENCIES
    now = time.time()
    LAST_API_LATENCY = latency
    API_LATENCIES.append((now, latency))
    API_LATENCIES = [(t, l) for (t, l) in API_LATENCIES if t >= now - 43200]


def get_latency_metrics():
    if not API_LATENCIES:
        return None, None
    last = LAST_API_LATENCY
    avg = sum(l for _, l in API_LATENCIES) / len(API_LATENCIES)
    return last, avg


def get_latency_extremes():
    if not API_LATENCIES:
        return None, None
    values = [l for _, l in API_LATENCIES]
    return max(values), min(values)


def fetch_events():
    global API_CHECKS_TODAY, API_SUCCESSES_TODAY, LAST_SUCCESSFUL_SYNC

    API_CHECKS_TODAY += 1
    start = time.time()
    logger.info("Fetching events from TruckersMP API")

    try:
        response = session.get(API_URL, timeout=25)
    except Exception as e:
        report_error("API Request Failed", "The TruckersMP API did not respond.", str(e))
        return None

    elapsed = time.time() - start
    record_latency(elapsed)
    logger.info(f"API response time: {elapsed:.2f}s")

    if response.status_code != 200:
        report_error("API HTTP Error", f"Status {response.status_code}", response.text[:1500])
        return None

    try:
        data = response.json()
    except Exception:
        report_error("Invalid JSON Response", "Malformed JSON.", response.text[:1000])
        return None

    if data.get("error") or "response" not in data:
        report_error("Invalid API Structure", "Missing expected fields.", json.dumps(data, indent=2)[:1500])
        return None

    events = data.get("response", [])
    API_SUCCESSES_TODAY += 1
    LAST_SUCCESSFUL_SYNC = datetime.now(timezone.utc)
    logger.info(f"Retrieved {len(events)} events")

    return {str(ev["id"]): ev for ev in events}


def discord_timestamp(dt_str, style="F"):
    try:
        dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        return f"<t:{int(dt.timestamp())}:{style}>"
    except Exception:
        return dt_str


def compare_events(old_event, new_event):
    fields = {
        "name": "Name",
        "start_at": "Start Time",
        "meetup_at": "Meetup Time",
        "server": "Server",
        "map": "Map",
        "banner": "Banner",
        "departure": "Start Location",
        "arrive": "End Location",
        "description": "Description",
    }

    diffs = {}

    for key, label in fields.items():
        old_val = old_event.get(key)
        new_val = new_event.get(key)

        if key == "server":
            old_val = old_val.get("name") if isinstance(old_val, dict) else old_val
            new_val = new_val.get("name") if isinstance(new_val, dict) else new_val

        if key in ("departure", "arrive"):
            if isinstance(old_val, dict):
                old_val = f"{old_val.get('city')} ({old_val.get('location')})"
            if isinstance(new_val, dict):
                new_val = f"{new_val.get('city')} ({new_val.get('location')})"

        if key in ("start_at", "meetup_at"):
            if old_val:
                old_val = discord_timestamp(old_val, "F")
            if new_val:
                new_val = discord_timestamp(new_val, "F")

        if old_val != new_val:
            diffs[label] = (old_val, new_val)

    return diffs


def build_embed(event, change_type="created", diffs=None):
    meetup_time = discord_timestamp(event.get("meetup_at"), "F") if event.get("meetup_at") else "Not specified"
    departure_time = discord_timestamp(event.get("start_at"), "F") if event.get("start_at") else "Not specified"
    event_date = discord_timestamp(event.get("start_at"), "F")

    start_location = f"{event['departure']['city']} ({event['departure']['location']})"
    end_location = f"{event['arrive']['city']} ({event['arrive']['location']})"

    if change_type == "created":
        title = f"🆕 Event Added: **{event['name']}**"
        color = 0x2ECC71
    elif change_type == "removed":
        title = f"❌ Event Removed: **{event['name']}**"
        color = 0xE74C3C
    else:
        title = f"🔄 Event Updated: **{event['name']}**"
        color = 0xE67E22

    embed = {
        "username": "TruckersMP Events Bot",
        "embeds": [
            {
                "title": title,
                "url": f"https://truckersmp.com{event['url']}",
                "color": color,
                "description": (
                    f"✨ **Organized by:** {event['vtc']['name']}\n"
                    f"🎮 **Game:** {event['game']}\n"
                    f"🔗 [View Event](https://truckersmp.com{event['url']})"
                ),
                "fields": [
                    {"name": "📅 Date", "value": event_date, "inline": False},
                    {"name": "🕒 Meetup Time", "value": meetup_time, "inline": True},
                    {"name": "🚦 Departure Time", "value": departure_time, "inline": True},
                    {"name": "🌍 Server", "value": event['server']['name'], "inline": False},
                    {"name": "🚩 Start Location", "value": start_location, "inline": False},
                    {"name": "🏁 End Location", "value": end_location, "inline": False},
                ],
                "image": {"url": event.get("map", "")},
                "thumbnail": {"url": event.get("banner", "")},
                "footer": {"text": f"Event ID: {event['id']}"},
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        ],
    }

    if change_type == "updated" and diffs:
        diff_text = "\n".join(
            f"**{label}:** `{old}` → `{new}`" if label != "Description" else "📝 **Description changed**"
            for label, (old, new) in diffs.items()
        )
        embed["embeds"][0]["fields"].append({"name": "🔧 Changes", "value": diff_text, "inline": False})

    return embed

def send_to_discord(event, change_type, diffs=None):
    embed = build_embed(event, change_type, diffs)

    try:
        result = session.post(WEBHOOK_URL, json=embed, timeout=20)
    except Exception as e:
        report_error("Discord Webhook Failure", f"Failed to send event: {event['name']}", str(e))
        return

    if result.status_code not in (200, 204):
        report_error("Discord Webhook Error", f"Discord rejected event: {event['name']}", result.text)
    else:
        logger.info(f"Sent {change_type} notification for event: {event['name']}")


def detect_changes(old_db, new_db):
    global TOTAL_ACTIVE_EVENTS, EVENTS_ADDED_TODAY, EVENTS_UPDATED_TODAY, EVENTS_REMOVED_TODAY

    changes = []

    for event_id, event in new_db.items():
        if event_id not in old_db:
            changes.append(("created", event, None))
            EVENTS_ADDED_TODAY += 1
        else:
            diffs = compare_events(old_db[event_id], event)
            if diffs:
                changes.append(("updated", event, diffs))
                EVENTS_UPDATED_TODAY += 1

    for event_id, event in old_db.items():
        if event_id not in new_db:
            changes.append(("removed", event, None))
            EVENTS_REMOVED_TODAY += 1

    TOTAL_ACTIVE_EVENTS = len(new_db)
    logger.info(
        f"Change detection complete — added: {EVENTS_ADDED_TODAY}, "
        f"updated: {EVENTS_UPDATED_TODAY}, removed: {EVENTS_REMOVED_TODAY}, "
        f"total active: {TOTAL_ACTIVE_EVENTS}"
    )
    return changes


def send_heartbeat():
    global HEARTBEATS_SENT_TODAY

    if not HEARTBEAT_WEBHOOK_URL:
        return

    last_latency, avg_latency = get_latency_metrics()

    fields = [
        {"name": "Heartbeat Time", "value": f"<t:{int(time.time())}:F>", "inline": False}
    ]

    if last_latency is not None:
        fields.append({"name": "Last API latency", "value": f"{last_latency:.2f} seconds", "inline": True})

    if avg_latency is not None:
        fields.append({"name": "12h average API latency", "value": f"{avg_latency:.2f} seconds", "inline": True})

    embed = {
        "username": "TruckersMP Events Bot — Heartbeat",
        "embeds": [
            {
                "title": "💓 Heartbeat",
                "color": 0x770202,
                "description": "The bot is running normally.",
                "fields": fields,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        ],
    }

    try:
        session.post(HEARTBEAT_WEBHOOK_URL, json=embed, timeout=15)
        HEARTBEATS_SENT_TODAY += 1
        logger.info("Heartbeat sent")
    except Exception as e:
        report_error("Heartbeat Send Failure", "Failed to send heartbeat message.", str(e))


def send_daily_summary():
    if not DAILY_SUMMARY_WEBHOOK_URL:
        return

    now_utc = datetime.now(timezone.utc)
    unix_now = int(now_utc.timestamp())

    last_latency, avg_latency = get_latency_metrics()
    max_latency, min_latency = get_latency_extremes()

    if API_CHECKS_TODAY > 0:
        api_uptime = (API_SUCCESSES_TODAY / API_CHECKS_TODAY) * 100
    else:
        api_uptime = None

    if LAST_SUCCESSFUL_SYNC is not None:
        last_sync_unix = int(LAST_SUCCESSFUL_SYNC.timestamp())
        last_sync_value = f"<t:{last_sync_unix}:R>"
    else:
        last_sync_value = "No successful sync yet"

    api_health_lines = []
    if api_uptime is not None:
        api_health_lines.append(f"• Uptime: {api_uptime:.1f}%")
    else:
        api_health_lines.append("• Uptime: N/A")

    if avg_latency is not None:
        api_health_lines.append(f"• Avg latency: {avg_latency:.2f}s")
    else:
        api_health_lines.append("• Avg latency: N/A")

    if max_latency is not None:
        api_health_lines.append(f"• Max latency: {max_latency:.2f}s")
    else:
        api_health_lines.append("• Max latency: N/A")

    if min_latency is not None:
        api_health_lines.append(f"• Min latency: {min_latency:.2f}s")
    else:
        api_health_lines.append("• Min latency: N/A")

    if API_SUCCESSES_TODAY == 0 and API_CHECKS_TODAY > 0:
        api_status = "🔴 Down / Unstable"
    elif API_SUCCESSES_TODAY > 0:
        api_status = "🟢 Stable"
    else:
        api_status = "⚪ No data"

    api_health_lines.append(f"• Status: {api_status}")

    event_activity_lines = [
        f"• Added: {EVENTS_ADDED_TODAY}",
        f"• Updated: {EVENTS_UPDATED_TODAY}",
        f"• Removed: {EVENTS_REMOVED_TODAY}",
        f"• Total active events: {TOTAL_ACTIVE_EVENTS}",
    ]

    vtc_highlights_lines = [
        f"• New ATM convoys: {EVENTS_ADDED_TODAY}",
        f"• Updated ATM convoys: {EVENTS_UPDATED_TODAY}",
        f"• Removed ATM convoys: {EVENTS_REMOVED_TODAY}",
    ]

    bot_health_lines = [
        f"• Restarts today: {RESTARTS_TODAY}",
        f"• Heartbeats sent: {HEARTBEATS_SENT_TODAY}",
        f"• Errors reported: {ERRORS_REPORTED_TODAY}",
    ]

    embed = {
        "username": "At The Mile Logistics — Daily Summary",
        "embeds": [
            {
                "title": "📊 At The Mile Logistics — Daily Summary",
                "color": ATM_COLOR,
                "description": f"Generated at: <t:{unix_now}:F>",
                "fields": [
                    {"name": "🟥 API Health", "value": "\n".join(api_health_lines), "inline": False},
                    {"name": "🚚 Event Activity", "value": "\n".join(event_activity_lines), "inline": False},
                    {"name": "🏢 ATM VTC Highlights", "value": "\n".join(vtc_highlights_lines), "inline": False},
                    {"name": "🧠 Bot Health", "value": "\n".join(bot_health_lines), "inline": False},
                    {"name": "🕒 Last successful sync", "value": last_sync_value, "inline": False},
                ],
                "thumbnail": {"url": ATM_LOGO_URL},
                "image": {"url": ATM_BANNER_URL},
                "timestamp": now_utc.isoformat(),
                "footer": {"text": "At The Mile Logistics — Going the Distance"},
            }
        ],
    }

    try:
        session.post(DAILY_SUMMARY_WEBHOOK_URL, json=embed, timeout=20)
        logger.info("Daily summary sent")
    except Exception as e:
        report_error("Daily Summary Failure", "Failed to send daily summary.", str(e))


def reset_daily_counters():
    global EVENTS_ADDED_TODAY, EVENTS_UPDATED_TODAY, EVENTS_REMOVED_TODAY
    global API_CHECKS_TODAY, API_SUCCESSES_TODAY
    global HEARTBEATS_SENT_TODAY, ERRORS_REPORTED_TODAY
    global RESTARTS_TODAY

    EVENTS_ADDED_TODAY = 0
    EVENTS_UPDATED_TODAY = 0
    EVENTS_REMOVED_TODAY = 0

    API_CHECKS_TODAY = 0
    API_SUCCESSES_TODAY = 0

    HEARTBEATS_SENT_TODAY = 0
    ERRORS_REPORTED_TODAY = 0

    RESTARTS_TODAY = 0
    logger.info("Daily counters reset")


def run_startup_self_test():
    now_utc = datetime.now(timezone.utc)
    unix_now = int(now_utc.timestamp())

    checks = {
        "Discord Webhook": WEBHOOK_URL is not None,
        "Error Webhook": ERROR_WEBHOOK_URL is not None,
        "Heartbeat Webhook": HEARTBEAT_WEBHOOK_URL is not None,
        "Daily Summary Webhook": DAILY_SUMMARY_WEBHOOK_URL is not None,
        "Branding Logo URL": ATM_LOGO_URL is not None,
        "Branding Banner URL": ATM_BANNER_URL is not None,
        "Database File": os.path.exists(DB_FILE),
    }

    try:
        api_test = session.get(API_URL, timeout=10)
        checks["TruckersMP API"] = api_test.status_code == 200
    except Exception:
        checks["TruckersMP API"] = False

    def status_icon(ok):
        return "🟢 OK" if ok else "🔴 FAILED"

    results_text = "\n".join([f"• **{name}:** {status_icon(ok)}" for name, ok in checks.items()])

    embed = {
        "username": "At The Mile Logistics — Startup Self‑Test",
        "embeds": [
            {
                "title": "🧪 ATM Bot Startup Self‑Test",
                "color": ATM_COLOR,
                "description": (
                    "The bot has started and completed a full system diagnostic.\n\n"
                    f"{results_text}\n\n"
                    f"**Startup Time:** <t:{unix_now}:F>"
                ),
                "thumbnail": {"url": ATM_LOGO_URL},
                "image": {"url": ATM_BANNER_URL},
                "footer": {"text": "At The Mile Logistics — Going the Distance"},
                "timestamp": now_utc.isoformat(),
            }
        ],
    }

    try:
        if DAILY_SUMMARY_WEBHOOK_URL:
            session.post(DAILY_SUMMARY_WEBHOOK_URL, json=embed, timeout=15)
        logger.info("Startup self-test completed")
    except Exception as e:
        logger.error(f"Failed to send startup self-test: {e}")


# ---------- Flask debug server ----------

app = Flask(__name__)


@app.route("/debug/db", methods=["GET"])
def debug_db():
    try:
        data = load_db()
        return jsonify(data), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/debug/logs", methods=["GET"])
def debug_logs():
    try:
        return jsonify(list(LOG_BUFFER)), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def start_debug_server():
    logger.info("Starting HTTP debug server on port 8080")
    app.run(host="0.0.0.0", port=8080)


# ---------- Main loop ----------

def main():
    global LAST_SUMMARY_DATE, RESTARTS_TODAY

    logger.info("TruckersMP Event Bot started")

    ensure_database_exists()
    run_startup_self_test()

    now_utc = datetime.now(timezone.utc)
    LAST_SUMMARY_DATE = now_utc.date().isoformat()

    old_db = load_db()
    new_db = fetch_events()

    if new_db is None:
        logger.warning("API unreachable at startup — using empty baseline")
        new_db = {}

    if not old_db:
        save_db(new_db)
    else:
        changes = detect_changes(old_db, new_db)
        if changes:
            logger.info(f"Applying {len(changes)} startup change(s)")
            for change_type, event, diffs in changes:
                send_to_discord(event, change_type, diffs)
            save_db(new_db)

    last_heartbeat_time = START_TIME

    try:
        while True:
            time.sleep(60)

            now = time.time()
            now_utc = datetime.now(timezone.utc)
            logger.info("Loop tick — checking for updates")

            old_db = load_db()
            new_db = fetch_events()

            if new_db is not None:
                changes = detect_changes(old_db, new_db)
                if changes:
                    logger.info(f"Detected {len(changes)} change(s)")
                    for change_type, event, diffs in changes:
                        send_to_discord(event, change_type, diffs)
                    save_db(new_db)
                else:
                    logger.info("No changes detected")
            else:
                logger.warning("API returned no data this loop")

            if now - last_heartbeat_time >= 1800:
                send_heartbeat()
                last_heartbeat_time = now

            today_iso = now_utc.date().isoformat()
            if now_utc.hour == 0 and now_utc.minute == 0 and LAST_SUMMARY_DATE != today_iso:
                send_daily_summary()
                LAST_SUMMARY_DATE = today_iso
                reset_daily_counters()

            if should_restart():
                RESTARTS_TODAY += 1
                logger.warning("Restarting bot due to 24h uptime limit")
                os.execv(sys.executable, [sys.executable] + sys.argv)

    except KeyboardInterrupt:
        logger.info("Bot stopped via KeyboardInterrupt")
        sys.exit(0)


if __name__ == "__main__":
    threading.Thread(target=start_debug_server, daemon=True).start()
    logger.info("HTTP debug server thread started")

    try:
        main()
    except Exception as e:
        logger.critical(f"Fatal bot error: {e}")
        report_error("Fatal Bot Error", "The bot crashed unexpectedly.", f"{type(e).__name__}: {e}")
        raise
