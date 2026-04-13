import requests
import time
import json
import os
import sys
import io
from datetime import datetime, timezone

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

API_URL = "https://api.truckersmp.com/v2/vtc/49940/events/attending"
WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]

DB_FILE = "events_db.json"
START_TIME = time.time()


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


def fetch_events():
    try:
        response = requests.get(API_URL, timeout=15)
    except requests.RequestException as e:
        print(f"[DEBUG] API request failed: {e}", flush=True)
        return {}

    print(f"[DEBUG] API status: {response.status_code}", flush=True)

    if response.status_code != 200:
        print(f"[DEBUG] API error: {response.text}", flush=True)
        return {}

    try:
        data = response.json()
    except ValueError:
        print("[DEBUG] API response was not valid JSON", flush=True)
        return {}

    print(f"[DEBUG] API response keys: {list(data.keys())}", flush=True)

    if not data.get("error") and data.get("response"):
        print(f"[DEBUG] Got {len(data['response'])} events", flush=True)
        return {str(ev["id"]): ev for ev in data["response"]}

    print("[DEBUG] API returned no events", flush=True)
    return {}


def discord_timestamp(dt_str, style="F"):
    try:
        dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        unix_ts = int(dt.timestamp())
        return f"<t:{unix_ts}:{style}>"
    except Exception:
        return dt_str


def build_embed(event, change_type="created", diffs=None):
    meetup_time = (
        discord_timestamp(event["meetup_at"], "F")
        if event.get("meetup_at")
        else "Not specified"
    )
    departure_time = (
        discord_timestamp(event["start_at"], "F")
        if event.get("start_at")
        else "Not specified"
    )
    event_date = discord_timestamp(event["start_at"], "F")

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
                    f"🔗 [View Event on TruckersMP](https://truckersmp.com{event['url']})"
                ),
                "fields": [
                    {"name": "📅 Date", "value": f"{event_date}", "inline": False},
                    {"name": "🕒 Meetup Time", "value": f"{meetup_time}", "inline": True},
                    {
                        "name": "🚦 Departure Time",
                        "value": f"{departure_time}",
                        "inline": True,
                    },
                    {
                        "name": "🌍 Server",
                        "value": f"**{event['server']['name']}**",
                        "inline": False,
                    },
                    {
                        "name": "🚩 Start Location",
                        "value": start_location,
                        "inline": False,
                    },
                    {"name": "🏁 End Location", "value": end_location, "inline": False},
                ],
                "image": {"url": event.get("map", "")},
                "thumbnail": {"url": event.get("banner", "")},
                "footer": {"text": f"Event ID: {event['id']} | TruckersMP API"},
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        ],
    }

    if change_type == "updated" and diffs:
        diff_lines = []
        for field, (old, new) in diffs.items():
            if field == "Description":
                diff_lines.append("📝 **Description changed**")
            else:
                diff_lines.append(f"**{field}:** `{old}` -> `{new}`")

        diff_text = "\n".join(diff_lines)
        embed["embeds"][0]["fields"].append(
            {"name": "🔧 Changes", "value": diff_text or "No details", "inline": False}
        )

    return embed


def send_to_discord(event, change_type, diffs=None):
    embed = build_embed(event, change_type, diffs)

    try:
        result = requests.post(WEBHOOK_URL, json=embed, timeout=15)
    except requests.RequestException as e:
        print(f"Failed to send event: {e}", flush=True)
        return

    if result.status_code in (200, 204):
        print(f"Sent {change_type} event: {event['name']}", flush=True)
    else:
        print(f"Failed to send event: {result.text}", flush=True)


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
                print(
                    f"[LOG] Event {new_event['id']} ({new_event['name']}) description changed:\n"
                    f"OLD: {old_val}\n"
                    f"NEW: {new_val}\n",
                    flush=True,
                )
                diffs[label] = (old_val, new_val)
            continue

        if old_val != new_val:
            diffs[label] = (old_val, new_val)

    return diffs


def main():
    print("TruckersMP Event Bot started...", flush=True)

    old_db = load_db()
    new_db = fetch_events()
    print(
        f"[DEBUG] Loaded {len(old_db)} old events, {len(new_db)} new events", flush=True
    )

    if not old_db:
        print("No database found. Creating baseline...", flush=True)
        save_db(new_db)
    else:
        changes = detect_changes(old_db, new_db)
        for change_type, event, diffs in changes:
            send_to_discord(event, change_type, diffs)
        if changes:
            save_db(new_db)

    try:
        while True:
            time.sleep(300)
            print(
                f"[{datetime.now(timezone.utc).isoformat()}] Checking for updates...",
                flush=True,
            )

            old_db = load_db()
            new_db = fetch_events()
            changes = detect_changes(old_db, new_db)

            if changes:
                for change_type, event, diffs in changes:
                    send_to_discord(event, change_type, diffs)
                save_db(new_db)
            else:
                print("No changes detected.", flush=True)

            if should_restart():
                print("24 hours passed. Restarting bot...", flush=True)
                os.execv(sys.executable, [sys.executable] + sys.argv)

    except KeyboardInterrupt:
        print("\n[INFO] Bot stopped by user. Exiting cleanly.", flush=True)
        sys.exit(0)


if __name__ == "__main__":
    main()
