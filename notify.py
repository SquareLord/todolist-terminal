#!/usr/bin/env python3
"""
GitHub Actions notification checker for taskman.
Reads tasks.json, fires ntfy.sh for due/overdue tasks,
and writes notified.json to track what's already been sent.

Required env vars (set as GitHub Actions secrets):
  NTFY_TOPIC   — your ntfy.sh topic name
  NTFY_SERVER  — (optional) defaults to https://ntfy.sh
"""

import json, os, sys, urllib.request, urllib.error
from datetime import datetime
from pathlib import Path

NTFY_TOPIC  = os.environ.get("NTFY_TOPIC", "").strip()
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
NOTIFY_AHEAD_MIN = 15   # warn this many minutes before due

TASKS_FILE    = Path("tasks.json")
NOTIFIED_FILE = Path("notified.json")


def load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return default


def days_left(due_str):
    if not due_str:
        return None
    try:
        return (datetime.fromisoformat(due_str) - datetime.now()).total_seconds() / 86400
    except ValueError:
        return None


def ntfy(title: str, body: str, priority: str = "default"):
    if not NTFY_TOPIC:
        print(f"[SKIP] No NTFY_TOPIC set — would have sent: {title}")
        return
    try:
        req = urllib.request.Request(
            f"{NTFY_SERVER}/{NTFY_TOPIC}",
            data=body.encode() if body else title.encode(),
            headers={
                "Title":    title,
                "Priority": priority,
                "Tags":     "bell,taskman",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10):
            pass
        print(f"[SENT] {title}")
    except Exception as e:
        print(f"[ERROR] ntfy failed: {e}", file=sys.stderr)


def main():
    tasks    = load_json(TASKS_FILE, [])
    notified = load_json(NOTIFIED_FILE, {})   # {task_id: {overdue: bool, due: bool}}
    now      = datetime.now()
    today    = now.date().isoformat()
    changed  = False

    # Clear stale entries for tasks no longer in the file
    active_ids = {str(t["id"]) for t in tasks}
    for k in list(notified.keys()):
        if k not in active_ids:
            del notified[k]
            changed = True

    # Reset daily digest flag at midnight (new day)
    last_date = notified.get("_date")
    if last_date != today:
        notified["_date"] = today
        # Clear today/digest flags so morning digest re-fires
        for k, v in notified.items():
            if isinstance(v, dict):
                v.pop("digest", None)
        changed = True

    # Morning digest at 9 AM: tasks due today
    hour = now.hour
    if 9 <= hour < 10:
        due_today = [t for t in tasks
                     if t.get("due_at") and t["due_at"][:10] == today
                     and t["status"] != "done"]
        if due_today:
            tid = "_digest"
            if not notified.get(tid, {}).get("sent"):
                titles = "\n".join(f"• {t['title']}" for t in due_today[:8])
                ntfy(f"📅 {len(due_today)} task(s) due today", titles, priority="default")
                notified.setdefault(tid, {})["sent"] = today
                changed = True

    for task in tasks:
        tid   = str(task["id"])
        dl    = days_left(task.get("due_at"))
        entry = notified.setdefault(tid, {})

        # Overdue
        if dl is not None and dl < 0 and not entry.get("overdue"):
            subject = task.get("subject") or ""
            label   = f"[{subject}] " if subject else ""
            ntfy(f"⚠ OVERDUE: {label}{task['title']}",
                 f"Was due {task['due_at']}", priority="high")
            entry["overdue"] = True
            changed = True

        # Due soon (within NOTIFY_AHEAD_MIN minutes)
        elif dl is not None and 0 <= dl <= NOTIFY_AHEAD_MIN / 1440 and not entry.get("due"):
            mins = int(dl * 1440)
            subject = task.get("subject") or ""
            label   = f"[{subject}] " if subject else ""
            ntfy(f"⏰ Due in {mins}m: {label}{task['title']}",
                 f"Due at {task['due_at']}", priority="urgent")
            entry["due"] = True
            changed = True

    if changed:
        NOTIFIED_FILE.write_text(json.dumps(notified, indent=2))
        print(f"Updated {NOTIFIED_FILE}")
    else:
        print("Nothing to notify.")


if __name__ == "__main__":
    main()
