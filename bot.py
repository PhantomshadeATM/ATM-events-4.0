import requests
import time
import json
import os
import sys
import io
from datetime import datetime, timezone
from requests.adapters import HTTPAdapter, Retry

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

API_URL = "https://api.truckersmp.com/v2/vtc/49940/events/attending"
WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]
ERROR_WEBHOOK_URL = os.environ.get("DISCORD_ERROR_WEBHOOK_URL")
HEARTBEAT_WEBHOOK_URL = os.environ.get("DISCORD_HEARTBEAT_WEBHOOK_URL")

DB_FILE = "events_db.json"
START_TIME = time.time()

API_LATENCIES = []
LAST_API_LATENCY = None

session = requests.Session()
retries = Retry(
    total=5,
    backoff_factor=2,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["GET", "POST"]
)
session.mount("https://", HTTPAdapter(max_retries=retries))


def should_restart():
    return (time.time() - START_TIME) >= 86400


def load_db():
    try:
        with open(DB_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_db(data):
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def report_error(title, message, details=None):
    if not ERROR_WEBHOOK_URL:
        print(f"[ERROR] {title}: {message}", flush=True)
        if details:
            print(details, flush=True)
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
        print(f"[ERROR] Failed to send error report: {e}", flush=True)


def record_latency(latency):
    global LAST_API_LATENCY, API_LATENCIES
    now = time.time()
    LAST_API_LATENCY = latency
    API_LATENCIES.append((now, latency))

    cutoff = now - 43200
    API_LATENCIES = [(t, l) for (t, l) in API_LATENCIES if t >= cutoff]


def get_latency_metrics():
    if not API_LATENCIES:
        return None, None
    last_latency = LAST_API_LATENCY
    avg_latency = sum(l for _, l in API_LATENCIES) / len(API_LATENCIES)
    return last_latency, avg_latency


def fetch_events():
    start = time.time()

    try:
        response = session.get(API_URL, timeout=25)
    except requests.RequestException as e:
        report_error("API Request Failed", "The TruckersMP API did not respond.", str(e))
        return None

    elapsed = time.time() - start
    record_latency(elapsed)
    print(f"[DEBUG] API response time: {elapsed:.2f}s", flush=True)

    if response.status_code != 200:
        report_error("API HTTP Error", f"Status {response.status_code}", response.text[:1500])
        return None

    try:
        data = response.json()
    except ValueError:
        report_error("Invalid JSON Response", "Malformed JSON.", response.text[:1000])
        return None

    if data.get("error") or "response" not in data:
        report_error("Invalid API Structure", "Missing expected fields.", json.dumps(data, indent=2)[:1500])
        return None

    events = data.get("response", [])
    print(f"[DEBUG] Got {len(events)} events", flush=True)

    return {str(ev["id"]): ev for ev in events}


def discord_timestamp(dt_str, style="F"):
    try:
        dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        unix_ts = int(dt.timestamp())
        return f"<t:{unix_ts}:{style}>"
    except Exception:
        return dt_str


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
    except requests.RequestException as e:
        report_error("Discord Webhook Failure", f"Failed to send event: {event['name']}", str(e))
        return

    if result.status_code not in (200, 204):
        report_error("Discord Webhook Error", f"Discord rejected event: {event['name']}", result.text)


def detect_changes(old_db, new_db):
    changes = []

    for event_id, event in new_db.items():
        if event_id not in old_db:
            changes.append(("created", event, None))
        else:
            diffs = compare_events(old_db[event_id], event)
            if diffs:
                changes.append(("updated", event, diffs))

    for event_id, event in old_db.items():
        if event_id not in new_db:
            changes.append(("removed", event, None))

    return changes


def compare_events(old_event, new_event):
    fields_to_check = {
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

    for key, label in fields_to_check.items():
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

        if key == "description":
            if old_val != new_val:
                diffs[label] = (old_val, new_val)
            continue

        if old_val != new_val:
            diffs[label] = (old_val, new_val)

    return diffs


# ---------------------------------------------------------
# ⭐ UPDATED HEARTBEAT FUNCTION (timestamp instead of uptime)
# ---------------------------------------------------------
def send_heartbeat():
    last_latency, avg_latency = get_latency_metrics()

    fields = [
        {
            "name": "Heartbeat Time",
            "value": f"<t:{int(time.time())}:F>",
            "inline": False
        }
    ]

    if last_latency is not None:
        fields.append({
            "name": "Last API latency",
            "value": f"{last_latency:.2f} seconds",
            "inline": True
        })

    if avg_latency is not None:
        fields.append({
            "name": "12h average API latency",
            "value": f"{avg_latency:.2f} seconds",
            "inline": True
        })

    embed = {
        "username": "TruckersMP Events Bot — Heartbeat",
        "embeds": [
            {
                "title": "💓 Heartbeat",
                "color": 0x3498DB,
                "description": "The bot is running normally.",
                "fields": fields,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        ],
    }

    try:
        session.post(HEARTBEAT_WEBHOOK_URL, json=embed, timeout=15)
        print("[HEARTBEAT] Sent heartbeat to Discord.", flush=True)
    except Exception as e:
        report_error("Heartbeat Send Failure", "Failed to send heartbeat message.", str(e))


def main():
    print("TruckersMP Event Bot started...", flush=True)

    old_db = load_db()
    new_db = fetch_events()

    if new_db is None:
        print("[WARN] API unreachable at startup — using empty baseline.", flush=True)
        new_db = {}

    if not old_db:
        save_db(new_db)
    else:
        changes = detect_changes(old_db, new_db)
        for change_type, event, diffs in changes:
            send_to_discord(event, change_type, diffs)
        if changes:
            save_db(new_db)

    last_heartbeat_time = START_TIME

    try:
        while True:
            time.sleep(300)
            now = time.time()

            old_db = load_db()
            new_db = fetch_events()

            if new_db is not None:
                changes = detect_changes(old_db, new_db)
                if changes:
                    for change_type, event, diffs in changes:
                        send_to_discord(event, change_type, diffs)
                    save_db(new_db)

            if now - last_heartbeat_time >= 1800:  # 30 minutes
                send_heartbeat()
                last_heartbeat_time = now

            if should_restart():
                os.execv(sys.executable, [sys.executable] + sys.argv)

    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        report_error("Fatal Bot Error", "The bot crashed unexpectedly.", f"{type(e).__name__}: {e}")
        raise
