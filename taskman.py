#!/usr/bin/env python3
"""
taskman — Terminal task manager, modeled after the Notion Planner.

TUI:    taskman
Widget: taskman widget
CLI:    taskman add "Title" [--subject Career] [--importance high] [--due "2026-09-15 14:00"]
                            [--hours 2] [--kind deadline|habit|project|quick|waiting|someday] [--pin]
        taskman list [N]   [--subject NAME] [--all]
        taskman someday
        taskman status <id> [todo|wip|done]
        taskman rm <id>
        taskman ping <id>
        taskman subjects
        taskman daemon
"""

import os, sys, json, subprocess, threading, time, sqlite3, signal, argparse, shutil, math
import urllib.request, urllib.error
from pathlib import Path
from datetime import datetime, timedelta, date
from typing import Optional, List

# ─── Paths ─────────────────────────────────────────────────────────────────────

DATA_DIR         = Path.home() / ".local" / "share" / "taskman"
DB_PATH          = DATA_DIR / "tasks.db"
CONFIG_PATH      = DATA_DIR / "config.json"
NOTIFY_AHEAD_MIN = 15
DIGEST_HOUR      = 9

# ─── Config (ntfy topic, etc.) ─────────────────────────────────────────────────

def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text())
        except Exception:
            pass
    return {}

def save_config(cfg: dict):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))

# ─── Database ──────────────────────────────────────────────────────────────────

def get_db() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

KINDS = ("deadline", "habit", "project", "quick", "waiting", "someday")

def init_db():
    """Create / migrate the database."""
    with get_db() as conn:
        conn.executescript("""
            -- Subjects replace the old 'projects' concept (Personal/Career/MATH2460/…)
            CREATE TABLE IF NOT EXISTS subjects (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT NOT NULL UNIQUE,
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS tasks (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                subject_id   INTEGER REFERENCES subjects(id) ON DELETE SET NULL,
                parent_id    INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
                title        TEXT NOT NULL,
                notes        TEXT DEFAULT '',
                due_at       TEXT,           -- ISO datetime
                recur        TEXT,           -- null | daily | weekly | monthly
                importance   INTEGER DEFAULT 2,  -- 1=HIGH 2=MED 3=LOW
                status       TEXT DEFAULT 'todo',  -- todo | wip | done
                done_at      TEXT,
                est_hours    REAL,           -- estimated hours
                pinned       INTEGER DEFAULT 0,
                parking_lot  INTEGER DEFAULT 0,  -- legacy, migrated to kind
                kind         TEXT DEFAULT 'deadline',
                notified_due INTEGER DEFAULT 0,
                created_at   TEXT DEFAULT (datetime('now'))
            );
        """)
        # Migrations: add columns if they don't exist yet
        for col_sql in (
            "ALTER TABLE tasks ADD COLUMN kind TEXT DEFAULT 'deadline'",
        ):
            try:
                conn.execute(col_sql)
            except Exception:
                pass  # column already exists
        # Migrate parking_lot=1 → kind='someday'
        conn.execute("""
            UPDATE tasks SET kind='someday'
            WHERE parking_lot=1 AND (kind IS NULL OR kind='deadline')
        """)
        # Seed default subjects matching Notion
        for s in ("Personal", "Career", "MATH2460", "cmsc351"):
            conn.execute("INSERT OR IGNORE INTO subjects (name) VALUES (?)", (s,))

# ─── Computed fields ───────────────────────────────────────────────────────────

def days_left(due_str: Optional[str]) -> Optional[float]:
    """Days until due (negative = overdue). None if no due date."""
    if not due_str:
        return None
    try:
        return (datetime.fromisoformat(due_str) - datetime.now()).total_seconds() / 86400
    except ValueError:
        return None

def work_by(due_str: Optional[str], est_hours: Optional[float]) -> Optional[str]:
    """When you should start — due minus buffer based on estimated hours."""
    if not due_str:
        return None
    try:
        dt     = datetime.fromisoformat(due_str)
        hours  = est_hours or 1.0
        buffer = timedelta(hours=hours * 1.5)  # 1.5× buffer
        wb     = dt - buffer
        return wb.strftime("%b %d") if wb.date() != datetime.now().date() else "today"
    except ValueError:
        return None

def priority_score(importance: int, due_str: Optional[str]) -> float:
    """
    Priority Score — mirrors the Notion formula.
    High importance + close deadline = high score.
    importance_weight: HIGH=3, MED=2, LOW=1
    urgency: 1 / max(days_left, 0.25)   (caps at 4× for sub-6h tasks)
    """
    imp_w   = {1: 3.0, 2: 2.0, 3: 1.0}.get(importance, 2.0)
    dl      = days_left(due_str)
    if dl is None:
        urgency = 0.5   # no due date → low-urgency default
    elif dl < 0:
        urgency = 8.0   # overdue → maximum urgency
    else:
        urgency = 1.0 / max(dl, 0.25)
    return round(imp_w * urgency, 2)

# ─── Display helpers ───────────────────────────────────────────────────────────

IMP_LABEL = {1: "HIGH", 2: "MED ", 3: "LOW "}
IMP_ICON  = {1: "!!!", 2: "!! ", 3: "!  "}
STATUS_ICON = {"todo": "○", "wip": "◑", "done": "✓"}
STATUS_NEXT = {"todo": "wip", "wip": "done", "done": "todo"}  # cycle

def format_due(due_str: Optional[str], short=False) -> str:
    if not due_str:
        return ""
    try:
        dt   = datetime.fromisoformat(due_str)
        now  = datetime.now()
        secs = (dt - now).total_seconds()
        if secs < 0:
            s = abs(secs)
            if s < 3600:   return f"⚠ {int(s/60)}m ago"
            if s < 86400:  return f"⚠ {int(s/3600)}h ago"
            return         f"⚠ {int(s/86400)}d ago"
        if secs < 3600:    return f"in {int(secs/60)}m"
        if secs < 86400:   return f"in {int(secs/3600)}h"
        if short:          return dt.strftime("%m/%d")
        return             dt.strftime("%b %d %H:%M")
    except ValueError:
        return due_str

def is_overdue(due_str: Optional[str], status: str) -> bool:
    if not due_str or status == "done":
        return False
    try:
        return datetime.fromisoformat(due_str) < datetime.now()
    except ValueError:
        return False

# ─── Notification helpers ──────────────────────────────────────────────────────

def notify(title: str, body: str = "", urgency: str = "normal"):
    # Desktop notification (Linux)
    try:
        subprocess.Popen(
            ["notify-send", "-u", urgency, "-a", "taskman", title, body],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        pass

    # ntfy.sh phone notification
    cfg = load_config()
    topic = cfg.get("ntfy_topic", "").strip()
    if topic:
        ntfy_server = cfg.get("ntfy_server", "https://ntfy.sh")
        priority_map = {"low": "low", "normal": "default", "critical": "urgent"}
        try:
            # HTTP headers must be Latin-1 encodable; non-Latin-1 chars
            # (e.g. emoji like 📌) raise UnicodeEncodeError in urllib and
            # silently kill the request. Strip them to ASCII-safe equivalents.
            ntfy_title = title.encode("ascii", errors="ignore").decode("ascii").strip() or "taskman"
            req = urllib.request.Request(
                f"{ntfy_server}/{topic}",
                data=body.encode() if body else title.encode(),
                headers={
                    "Title": ntfy_title,
                    "Priority": priority_map.get(urgency, "default"),
                    "Tags": "bell",
                },
                method="POST",
            )
            urllib.request.urlopen(req, timeout=5)
        except Exception:
            pass  # never let ntfy failure block anything

# ─── Daemon ────────────────────────────────────────────────────────────────────

class NotificationDaemon(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True, name="taskman-notify")
        self._stop        = threading.Event()
        self._last_digest = None

    def stop(self): self._stop.set()

    def run(self):
        while not self._stop.wait(60):
            self._check_due()
            self._check_digest()

    def _check_due(self):
        try:
            conn = get_db()
            now  = datetime.now()
            soon = now + timedelta(minutes=NOTIFY_AHEAD_MIN)
            for row in conn.execute("""
                SELECT id, title, due_at FROM tasks
                WHERE status!='done' AND due_at IS NOT NULL
                  AND due_at < ? AND notified_due=0
            """, (now.isoformat(),)).fetchall():
                notify(f"⚠ Overdue: {row['title']}",
                       format_due(row['due_at']), urgency="critical")
                conn.execute("UPDATE tasks SET notified_due=1 WHERE id=?", (row['id'],))
            for row in conn.execute("""
                SELECT id, title, due_at FROM tasks
                WHERE status!='done' AND due_at IS NOT NULL
                  AND due_at>=? AND due_at<=? AND notified_due=0
            """, (now.isoformat(), soon.isoformat())).fetchall():
                notify(f"⏰ Due soon: {row['title']}", format_due(row['due_at']))
                conn.execute("UPDATE tasks SET notified_due=1 WHERE id=?", (row['id'],))
            conn.commit(); conn.close()
        except Exception:
            pass

    def _check_digest(self):
        now   = datetime.now()
        today = now.date()
        # Use >= so digest fires even if laptop was off at exactly DIGEST_HOUR
        if now.hour >= DIGEST_HOUR and self._last_digest != today:
            self._last_digest = today
            # Persist so daemon restart doesn't re-fire today's digest
            try:
                cfg = load_config()
                cfg["_last_digest_date"] = today.isoformat()
                save_config(cfg)
            except Exception:
                pass
            try:
                conn = get_db()
                rows = conn.execute("""
                    SELECT t.title, t.due_at, t.importance, s.name AS subject_name
                    FROM tasks t LEFT JOIN subjects s ON t.subject_id=s.id
                    WHERE t.status!='done' AND t.kind NOT IN ('someday','waiting')
                """).fetchall()
                conn.close()
                # Sort by priority score descending, take top 5
                ranked = sorted(
                    rows,
                    key=lambda r: priority_score(r["importance"], r["due_at"]),
                    reverse=True
                )[:5]
                for i, r in enumerate(ranked, 1):
                    due_label = format_due(r["due_at"], short=True)
                    subj      = r["subject_name"] or ""
                    body      = f"{subj} · {due_label}" if due_label else subj
                    urgency   = "critical" if is_overdue(r["due_at"], "todo") else "normal"
                    notify(f"#{i} {r['title']}", body, urgency)
            except Exception:
                pass

def run_daemon_foreground():
    print("taskman daemon running — Ctrl-C to stop", flush=True)
    d = NotificationDaemon()
    d.start()
    try:
        while True: time.sleep(1)
    except KeyboardInterrupt:
        d.stop(); print("\nDaemon stopped.")

# ─── CLI helpers ───────────────────────────────────────────────────────────────

def _get_or_create_subject(conn, name: str) -> int:
    row = conn.execute("SELECT id FROM subjects WHERE name=?", (name,)).fetchone()
    if row: return row["id"]
    conn.execute("INSERT INTO subjects (name) VALUES (?)", (name,))
    conn.commit()
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]

def _parse_importance(s: str) -> int:
    return {"high": 1, "h": 1, "med": 2, "medium": 2, "m": 2, "low": 3, "l": 3}.get(
        (s or "med").lower(), 2)

def _importance_str(i: int) -> str:
    return {1: "high", 2: "med", 3: "low"}.get(i, "med")

def _spawn_next_recurrence(conn, task):
    """Create the next occurrence of a recurring task."""
    if not (task["recur"] and task["due_at"]):
        return
    try:
        due   = datetime.fromisoformat(task["due_at"])
        delta = {"daily": timedelta(days=1), "weekly": timedelta(weeks=1),
                 "monthly": timedelta(days=30)}.get(task["recur"])
        if not delta: return
        nd = due + delta
        conn.execute("""
            INSERT INTO tasks (subject_id,title,notes,due_at,recur,importance,
                               est_hours,pinned,kind)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, (task["subject_id"], task["title"], task["notes"], nd.isoformat(),
              task["recur"], task["importance"], task["est_hours"],
              task["pinned"], task.get("kind", "habit")))
    except Exception:
        pass

# ─── CLI commands ──────────────────────────────────────────────────────────────

def _parse_date_time_flags(args) -> Optional[str]:
    """Parse --date MMDDYYYY [--time HHMMa|HHMMp] into an ISO due_at string."""
    date_str = getattr(args, "date", None)
    time_str = getattr(args, "time", None)
    if not date_str:
        return None
    try:
        if len(date_str) != 8:
            raise ValueError("date must be MMDDYYYY")
        mm, dd, yyyy = date_str[:2], date_str[2:4], date_str[4:]
        base = datetime.strptime(f"{yyyy}-{mm}-{dd}", "%Y-%m-%d")
    except ValueError:
        print(f"taskman: bad --date format '{date_str}', expected MMDDYYYY")
        return None
    if time_str:
        try:
            suffix = time_str[-1].lower()
            hhmm   = time_str[:-1]
            if len(hhmm) != 4 or suffix not in ("a", "p"):
                raise ValueError
            h, m = int(hhmm[:2]), int(hhmm[2:])
            if suffix == "p" and h != 12:
                h += 12
            if suffix == "a" and h == 12:
                h = 0
            base = base.replace(hour=h, minute=m)
        except (ValueError, IndexError):
            print(f"taskman: bad --time format '{time_str}', expected HHMMa or HHMMp")
            return None
    else:
        base = base.replace(hour=23, minute=59)
    return base.strftime("%Y-%m-%d %H:%M")

def cli_edit(args):
    """Interactive wizard to edit an existing task's fields."""
    init_db()
    conn = get_db()
    row  = conn.execute(
        "SELECT t.*, s.name AS subject_name FROM tasks t "
        "LEFT JOIN subjects s ON t.subject_id=s.id WHERE t.id=?",
        (args.id,)
    ).fetchone()
    if not row:
        print(f"Task #{args.id} not found.")
        conn.close()
        return
    task = dict(row)
    conn.close()

    print(f"\nEditing task #{task['id']}: {task['title']}")
    print("Press Enter to keep the current value.\n")

    updates = {}

    # Title
    cur = task["title"]
    ans = input(f"  Title [{cur}]: ").strip()
    if ans:
        updates["title"] = ans

    # Subject
    cur = task["subject_name"] or "Personal"
    ans = input(f"  Subject [{cur}]: ").strip()
    if ans:
        conn = get_db()
        updates["subject_id"] = _get_or_create_subject(conn, ans)
        conn.close()

    # Importance
    cur = _importance_str(task["importance"])
    ans = input(f"  Importance (high/med/low) [{cur}]: ").strip()
    if ans:
        updates["importance"] = _parse_importance(ans)

    # Due date
    cur = task["due_at"] or "none"
    ans = input(f"  Due (MMDDYYYY or YYYY-MM-DD HH:MM) [{cur}]: ").strip()
    if ans:
        if len(ans) == 8 and ans.isdigit():
            import types
            fake = types.SimpleNamespace(date=ans, time=None)
            parsed = _parse_date_time_flags(fake)
            updates["due_at"] = parsed or cur
        else:
            updates["due_at"] = parse_due(ans) or cur

    # Estimated hours
    cur = str(task["est_hours"]) if task["est_hours"] else "none"
    ans = input(f"  Est. hours [{cur}]: ").strip()
    if ans:
        try:
            updates["est_hours"] = float(ans)
        except ValueError:
            print("  (invalid number, skipping hours)")

    # Kind
    cur = task.get("kind") or "deadline"
    ans = input(f"  Kind ({'/'.join(KINDS)}) [{cur}]: ").strip().lower()
    if ans and ans in KINDS:
        updates["kind"] = ans
        if ans == "habit":
            cur_recur = task.get("recur") or "daily"
            r_ans = input(f"  How often? (daily/weekly/monthly) [{cur_recur}]: ").strip().lower()
            updates["recur"] = r_ans if r_ans in ("daily", "weekly", "monthly") else cur_recur
        elif ans == "waiting":
            w_ans = input("  What are you waiting on? ").strip()
            if w_ans:
                existing = updates.get("notes", task.get("notes", "") or "")
                updates["notes"] = f"Waiting on: {w_ans}" if not existing else f"{existing}\nWaiting on: {w_ans}"

    # Notes
    cur = task["notes"] or "none"
    ans = input(f"  Notes [{cur}]: ").strip()
    if ans:
        updates["notes"] = ans

    if not updates:
        print("\nNo changes made.")
        return

    set_clause = ", ".join(f"{k}=?" for k in updates)
    values     = list(updates.values()) + [task["id"]]
    conn = get_db()
    conn.execute(f"UPDATE tasks SET {set_clause} WHERE id=?", values)
    conn.commit()
    conn.close()
    print(f"\nTask #{task['id']} updated.")


def cli_add(args):
    init_db()
    conn       = get_db()
    subj_name  = args.subject or "Personal"
    subj_id    = _get_or_create_subject(conn, subj_name)
    due_at     = _parse_date_time_flags(args) or (parse_due(args.due) if args.due else None)
    importance = _parse_importance(args.importance or "med")
    kind       = getattr(args, "kind", None) or "deadline"
    pinned     = 1 if getattr(args, "pin", False) else 0
    recur      = None
    notes      = args.notes or ""

    # Interactive follow-ups based on kind
    if kind == "habit":
        recur_ans = input("How often? (daily/weekly/monthly) [daily]: ").strip().lower()
        recur = recur_ans if recur_ans in ("daily", "weekly", "monthly") else "daily"
    elif kind == "waiting":
        waiting_ans = input("What are you waiting on? ").strip()
        if waiting_ans:
            notes = f"Waiting on: {waiting_ans}" if not notes else f"{notes}\nWaiting on: {waiting_ans}"

    conn.execute("""
        INSERT INTO tasks (subject_id,title,notes,due_at,recur,importance,
                           est_hours,pinned,kind)
        VALUES (?,?,?,?,?,?,?,?,?)
    """, (subj_id, args.title, notes, due_at,
          recur, importance, args.hours, pinned, kind))
    conn.commit()
    tid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.close()
    flags = []
    if pinned:          flags.append("📌 pinned")
    if kind != "deadline": flags.append(f"[{kind}]")
    extra = f"  [{', '.join(flags)}]" if flags else ""
    print(f"✓ Added #{tid}: {args.title}{extra}")

def _build_task_query(subject=None, view="open", someday=False):
    """Build WHERE clause and params for task queries."""
    w, p = ["t.parent_id IS NULL"], []
    if someday:
        w.append("t.kind='someday'")
    else:
        w.append("t.kind NOT IN ('someday','waiting')")
    if view == "open":
        w.append("t.status!='done'")
    elif view == "done_today":
        w.append("t.status='done' AND date(t.done_at)=date('now')")
    elif view == "all":
        pass
    if subject:
        w.append("s.name=?"); p.append(subject)
    return " AND ".join(w), p

def cli_list(args):
    init_db()
    conn    = get_db()
    someday = getattr(args, "someday", False)
    view = "done_today" if getattr(args, "done_today", False) \
           else "all"   if getattr(args, "all", False) \
           else "open"
    where, params = _build_task_query(
        subject=getattr(args, "subject", None), view=view, someday=someday)

    rows = conn.execute(f"""
        SELECT t.*, s.name AS subject_name,
               ({priority_score.__doc__ and '0'}) AS _ph
        FROM tasks t LEFT JOIN subjects s ON t.subject_id=s.id
        WHERE {where}
    """.replace("({priority_score.__doc__ and '0'})", "0"), params).fetchall()
    conn.close()

    if not rows:
        print("No tasks."); return

    # Sort by: pinned desc, score desc, due_at asc
    def sort_key(r):
        score = priority_score(r["importance"], r["due_at"])
        pin   = -(r["pinned"] or 0)
        due   = r["due_at"] or "9999"
        return (pin, -score, due)

    rows = sorted([dict(r) for r in rows], key=sort_key)

    limit = getattr(args, "n", 5)
    # someday/done-today views: show all; --all flag: show all
    if limit and not getattr(args, "all", False) and not getattr(args, "someday", False) \
             and not getattr(args, "done_today", False):
        rows = rows[:limit]

    tw = shutil.get_terminal_size((60, 24)).columns
    print(f"{'ID':>4}  {'St':2}  {'Imp':4}  {'Title':<24}  {'Subj':<7}  {'Due/Wb':<9}")
    print("─" * min(tw, 60))
    for r in rows:
        st     = STATUS_ICON.get(r["status"], "○")
        imp    = IMP_LABEL.get(r["importance"], "MED ")
        title  = r["title"][:23]
        subj   = (r["subject_name"] or "")[:7]
        over   = is_overdue(r["due_at"], r["status"])
        wb     = work_by(r["due_at"], r["est_hours"])
        if wb and r["est_hours"]:
            display = f"\033[31m{wb}\033[0m" if over else wb
        else:
            due     = format_due(r["due_at"], short=True)
            display = f"\033[31m{due}\033[0m" if over else due
        print(f"{r['id']:>4}  {st:2}  {imp}  {title:<24}  {subj:<7}  {display:<9}")

def cli_list_backlog(args):
    """Grouped by subject — mirrors Notion Backlog view."""
    init_db()
    conn = get_db()
    rows = conn.execute("""
        SELECT t.*, s.name AS subject_name FROM tasks t
        LEFT JOIN subjects s ON t.subject_id=s.id
        WHERE t.status!='done' AND t.kind NOT IN ('someday','waiting') AND t.parent_id IS NULL
        ORDER BY s.name, t.importance ASC, t.due_at ASC NULLS LAST
    """).fetchall()
    conn.close()

    groups: dict = {}
    for r in rows:
        k = r["subject_name"] or "—"
        groups.setdefault(k, []).append(dict(r))

    for subj, tasks in sorted(groups.items()):
        hrs_total = sum(t["est_hours"] or 0 for t in tasks)
        print(f"\n  \033[1m{subj}\033[0m  ({len(tasks)} tasks"
              + (f", ~{hrs_total:.0f}h" if hrs_total else "") + ")")
        print("  " + "─" * 60)
        for t in sorted(tasks, key=lambda x: -priority_score(x["importance"], x["due_at"])):
            st    = STATUS_ICON.get(t["status"], "○")
            imp   = IMP_LABEL.get(t["importance"], "MED ")
            pin   = "📌 " if t["pinned"] else ""
            due   = format_due(t["due_at"], short=True)
            score = priority_score(t["importance"], t["due_at"])
            print(f"  {t['id']:>4}  {st} {imp} {pin}{t['title'][:40]:<42}  {due:<10}  {score:.1f}pt")

def cli_status(args):
    init_db()
    conn  = get_db()
    task  = conn.execute("SELECT * FROM tasks WHERE id=?", (args.id,)).fetchone()
    if not task:
        print(f"Task #{args.id} not found."); conn.close(); return

    new_status = args.new_status if args.new_status else STATUS_NEXT[task["status"]]
    done_at    = datetime.now().isoformat() if new_status == "done" else None
    conn.execute("UPDATE tasks SET status=?, done_at=? WHERE id=?",
                 (new_status, done_at, args.id))

    if new_status == "done":
        _spawn_next_recurrence(conn, dict(task))

    conn.commit(); conn.close()
    icon = STATUS_ICON.get(new_status, "?")
    print(f"{icon} #{args.id} {task['title']}  →  {new_status}")

def cli_rm(args):
    init_db()
    conn = get_db()
    task = conn.execute("SELECT title FROM tasks WHERE id=?", (args.id,)).fetchone()
    if not task: print(f"Task #{args.id} not found."); return
    conn.execute("DELETE FROM tasks WHERE id=?", (args.id,))
    conn.commit(); conn.close()
    print(f"Deleted #{args.id}: {task['title']}")

def cli_ping(args):
    init_db()
    conn = get_db()
    task = conn.execute("SELECT title, due_at FROM tasks WHERE id=?", (args.id,)).fetchone()
    conn.close()
    if not task: print(f"Task #{args.id} not found."); return
    notify(f"📌 {task['title']}", format_due(task["due_at"]))
    print(f"Pinged: {task['title']}")

def cli_subjects(args):
    init_db()
    conn = get_db()
    rows = conn.execute("""
        SELECT s.name,
               COUNT(CASE WHEN t.status!='done' AND t.kind NOT IN ('someday','waiting') THEN 1 END) AS open,
               COUNT(CASE WHEN t.kind='someday' THEN 1 END) AS someday,
               COUNT(CASE WHEN t.kind='waiting' THEN 1 END) AS waiting,
               COALESCE(SUM(CASE WHEN t.status!='done' THEN t.est_hours END),0) AS hrs
        FROM subjects s LEFT JOIN tasks t ON t.subject_id=s.id
        GROUP BY s.id ORDER BY s.name
    """).fetchall()
    conn.close()
    print(f"  {'Subject':<20} {'Open':>5}  {'Smdy':>5}  {'Wait':>5}  {'~Hrs':>5}")
    print("  " + "─" * 48)
    for r in rows:
        print(f"  {r['name']:<20} {r['open']:>5}  {r['someday']:>5}  {r['waiting']:>5}  {r['hrs']:>5.1f}h")

def parse_due(s: str) -> Optional[str]:
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try: return datetime.strptime(s.strip(), fmt).isoformat()
        except ValueError: pass
    return None

# ─── TUI ───────────────────────────────────────────────────────────────────────

try:
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Container, Horizontal, Vertical
    from textual.reactive import reactive
    from textual.screen import ModalScreen
    from textual.widgets import (
        Button, DataTable, Footer, Header,
        Input, Label, ListItem, ListView,
        Select, Static, TextArea,
    )
    HAS_TEXTUAL = True
except ImportError:
    HAS_TEXTUAL = False

if HAS_TEXTUAL:

    # ── Task modal ─────────────────────────────────────────────────────────────

    class TaskModal(ModalScreen):
        BINDINGS = [("escape", "dismiss(None)", "Cancel")]

        DEFAULT_CSS = """
        TaskModal { align: center middle; }
        #modal-box {
            background: $surface; border: thick $primary;
            padding: 1 2; width: 66; max-height: 38;
        }
        #modal-title { text-style: bold; margin-bottom: 1; }
        Label { margin-top: 1; }
        Input, Select { width: 100%; }
        TextArea { height: 4; }
        #row2 { height: auto; }
        #row2 > * { width: 1fr; margin-right: 1; }
        #modal-buttons { margin-top: 1; align: right middle; }
        Button { margin-left: 1; }
        """

        def __init__(self, task: Optional[dict] = None, subjects: List[dict] = None):
            super().__init__()
            self.task     = task or {}
            self.subjects = subjects or []

        def compose(self) -> ComposeResult:
            action = "Edit Task" if self.task else "Add Task"
            with Container(id="modal-box"):
                yield Label(action, id="modal-title")
                yield Label("Title")
                yield Input(value=self.task.get("title",""),
                            placeholder="What needs doing?", id="inp-title")
                with Horizontal(id="row2"):
                    with Vertical():
                        yield Label("Subject")
                        opts = [(s["name"], str(s["id"])) for s in self.subjects]
                        cur  = str(self.task.get("subject_id",
                               self.subjects[0]["id"] if self.subjects else ""))
                        yield Select(opts, value=cur, id="inp-subject")
                    with Vertical():
                        yield Label("Importance")
                        yield Select([("HIGH","1"),("MED","2"),("LOW","3")],
                                     value=str(self.task.get("importance",2)),
                                     id="inp-imp")
                    with Vertical():
                        yield Label("Status")
                        yield Select([("Not Started","todo"),("In Progress","wip"),
                                      ("Done","done")],
                                     value=self.task.get("status","todo"),
                                     id="inp-status")
                yield Label("Due  (YYYY-MM-DD HH:MM or blank)")
                due_s = ""
                if self.task.get("due_at"):
                    try: due_s = datetime.fromisoformat(self.task["due_at"]).strftime("%Y-%m-%d %H:%M")
                    except Exception: due_s = self.task.get("due_at","")
                yield Input(value=due_s, placeholder="2026-09-15 14:00", id="inp-due")
                with Horizontal(id="row2"):
                    with Vertical():
                        yield Label("Est. Hours")
                        yield Input(value=str(self.task.get("est_hours","") or ""),
                                    placeholder="e.g. 2.5", id="inp-hours")
                    with Vertical():
                        yield Label("Kind")
                        yield Select([("Deadline","deadline"),("Habit","habit"),
                                      ("Project","project"),("Quick","quick"),
                                      ("Waiting","waiting"),("Someday","someday")],
                                     value=self.task.get("kind","deadline") or "deadline",
                                     id="inp-kind")
                yield Label("Notes")
                yield TextArea(text=self.task.get("notes","") or "", id="inp-notes")
                with Horizontal(id="modal-buttons"):
                    yield Button("Save", variant="primary", id="btn-save")
                    yield Button("Cancel", id="btn-cancel")

        def on_button_pressed(self, event: Button.Pressed) -> None:
            if event.button.id == "btn-cancel":
                self.dismiss(None); return
            title = self.query_one("#inp-title", Input).value.strip()
            if not title: return
            due_s    = self.query_one("#inp-due", Input).value.strip()
            hours_s  = self.query_one("#inp-hours", Input).value.strip()
            subj_sel = self.query_one("#inp-subject", Select)
            imp_sel  = self.query_one("#inp-imp", Select)
            stat_sel = self.query_one("#inp-status", Select)
            kind_s   = self.query_one("#inp-kind", Select).value or "deadline"
            notes    = self.query_one("#inp-notes", TextArea).text
            try:    hours = float(hours_s) if hours_s else None
            except: hours = None
            # Auto-set recur=daily for habit kind
            recur = "daily" if kind_s == "habit" else None
            self.dismiss({
                "title":      title,
                "subject_id": int(subj_sel.value) if subj_sel.value else None,
                "importance": int(imp_sel.value),
                "status":     stat_sel.value,
                "due_at":     parse_due(due_s) if due_s else None,
                "kind":       kind_s,
                "recur":      recur,
                "est_hours":  hours,
                "notes":      notes,
            })

    class SubjectModal(ModalScreen):
        BINDINGS = [("escape", "dismiss(None)", "Cancel")]
        DEFAULT_CSS = """
        SubjectModal { align: center middle; }
        #sbox { background: $surface; border: thick $primary; padding: 1 2; width: 44; }
        Label { margin-top: 1; }
        Input { width: 100%; }
        #sbtns { margin-top: 1; align: right middle; }
        Button { margin-left: 1; }
        """
        def compose(self) -> ComposeResult:
            with Container(id="sbox"):
                yield Label("New Subject", id="modal-title")
                yield Label("Name")
                yield Input(placeholder="e.g. Research", id="inp-name")
                with Horizontal(id="sbtns"):
                    yield Button("Create", variant="primary", id="btn-save")
                    yield Button("Cancel", id="btn-cancel")
        def on_button_pressed(self, event: Button.Pressed) -> None:
            if event.button.id == "btn-cancel": self.dismiss(None); return
            name = self.query_one("#inp-name", Input).value.strip()
            self.dismiss(name if name else None)

    # ── Main App ───────────────────────────────────────────────────────────────

    # View modes — mirrors Notion's views
    VIEWS = ["backlog", "open", "today", "overdue", "done_today", "someday", "waiting", "all"]
    VIEW_LABELS = {
        "backlog":    "📋 Backlog",
        "open":       "📝 Open",
        "today":      "📅 Today",
        "overdue":    "⚠  Overdue",
        "done_today": "✓  Done Today",
        "someday":    "🌙 Someday",
        "waiting":    "⏳ Waiting",
        "all":        "🗂  All Tasks",
    }

    class TaskManApp(App):
        TITLE = "taskman"

        CSS = """
        #main { height: 1fr; }

        #sidebar {
            width: 24; border-right: solid $primary-darken-2;
            background: $surface-darken-1;
        }
        #sidebar-title, #sidebar-title-2 {
            background: $primary-darken-3; color: $text;
            text-style: bold; padding: 0 1; width: 100%;
        }
        #view-list { height: auto; border-bottom: solid $primary-darken-3; }
        #subject-list { height: 1fr; }
        ListView > ListItem { padding: 0 1; }
        ListView > ListItem.--highlight { background: $primary 30%; }

        #content { width: 1fr; }
        #filter-bar {
            background: $surface-darken-2; padding: 0 1;
            height: 1; color: $text-muted;
        }
        DataTable { height: 1fr; }
        DataTable > .datatable--cursor { background: $primary 40%; }
        DataTable > .datatable--header { text-style: bold; background: $surface-darken-2; }
        Footer { background: $surface-darken-2; }
        """

        BINDINGS = [
            Binding("a",     "add_task",         "Add"),
            Binding("e",     "edit_task",         "Edit"),
            Binding("space", "cycle_status",      "Status"),
            Binding("D",     "delete_task",       "Delete"),
            Binding("P",     "toggle_pin",        "Pin"),
            Binding("L",     "toggle_someday",     "Someday"),
            Binding("n",     "new_subject",       "Subject"),
            Binding("p",     "ping_task",         "Ping"),
            Binding("1",     "set_view('open')",  "Open"),
            Binding("2",     "set_view('backlog')","Backlog"),
            Binding("3",     "set_view('today')", "Today"),
            Binding("4",     "set_view('overdue')","Overdue"),
            Binding("5",     "set_view('done_today')","Done Today"),
            Binding("6",     "set_view('someday')","Someday"),
            Binding("7",     "set_view('waiting')","Waiting"),
            Binding("r",     "refresh",           "Refresh"),
            Binding("q",     "quit",              "Quit"),
        ]

        view_mode:       reactive[str]           = reactive("open")
        current_subject: reactive[Optional[int]] = reactive(None)
        _task_ids: list

        def __init__(self):
            super().__init__()
            self._task_ids = []
            self._daemon   = NotificationDaemon()

        def compose(self) -> ComposeResult:
            yield Header(show_clock=True)
            with Horizontal(id="main"):
                with Vertical(id="sidebar"):
                    yield Label(" VIEWS", id="sidebar-title")
                    yield ListView(id="view-list")
                    yield Label(" SUBJECTS", id="sidebar-title-2")
                    yield ListView(id="subject-list")
                with Vertical(id="content"):
                    yield Static("", id="filter-bar")
                    yield DataTable(id="task-table", cursor_type="row",
                                    zebra_stripes=True)
            yield Footer()

        def on_mount(self) -> None:
            init_db()
            self._daemon.start()
            t = self.query_one("#task-table", DataTable)
            t.add_columns("St", "Imp", "📌", "Title", "Subject", "Due",
                           "Hrs", "Score", "Work-by")
            self._populate_view_list()
            self.refresh_subjects()
            self.refresh_tasks()

        # ── Sidebar ───────────────────────────────────────────────────────────

        def _populate_view_list(self):
            lv = self.query_one("#view-list", ListView)
            lv.clear()
            for v in VIEWS:
                lv.append(ListItem(Label(f" {VIEW_LABELS[v]}"), id=f"view-{v}"))

        def refresh_subjects(self):
            c    = get_db()
            rows = [dict(r) for r in c.execute(
                "SELECT * FROM subjects ORDER BY name").fetchall()]
            c.close()
            lv = self.query_one("#subject-list", ListView)
            lv.clear()
            lv.append(ListItem(Label("  All subjects"), id="subj-all"))
            for s in rows:
                lv.append(ListItem(Label(f"  {s['name']}"), id=f"subj-{s['id']}"))
            self._subjects = rows

        # ── Data ──────────────────────────────────────────────────────────────

        def _get_subjects(self):
            c = get_db()
            r = [dict(x) for x in c.execute("SELECT * FROM subjects ORDER BY name").fetchall()]
            c.close(); return r

        def _get_tasks(self):
            c  = get_db()
            w  = ["t.parent_id IS NULL"]
            p  = []

            if self.view_mode == "open":
                w.append("t.status!='done' AND t.kind NOT IN ('someday','waiting')")
            elif self.view_mode == "today":
                w.append("t.status!='done' AND date(t.due_at)=date('now') AND t.kind NOT IN ('someday','waiting')")
            elif self.view_mode == "overdue":
                w.append("t.status!='done' AND t.due_at<datetime('now') AND t.kind NOT IN ('someday','waiting')")
            elif self.view_mode == "done_today":
                w.append("t.status='done' AND date(t.done_at)=date('now')")
            elif self.view_mode == "someday":
                w.append("t.kind='someday'")
            elif self.view_mode == "waiting":
                w.append("t.kind='waiting' AND t.status!='done'")
            elif self.view_mode == "backlog":
                w.append("t.status!='done' AND t.kind NOT IN ('someday','waiting')")
            # "all" — no extra filter

            if self.current_subject is not None:
                w.append("t.subject_id=?"); p.append(self.current_subject)

            rows = c.execute(f"""
                SELECT t.*, s.name AS subject_name
                FROM tasks t LEFT JOIN subjects s ON t.subject_id=s.id
                WHERE {" AND ".join(w)}
            """, p).fetchall()
            c.close()
            tasks = [dict(r) for r in rows]

            # Sort: pinned → priority score desc → due asc
            tasks.sort(key=lambda t: (
                -(t["pinned"] or 0),
                -priority_score(t["importance"], t["due_at"]),
                t["due_at"] or "9999",
            ))
            return tasks

        def refresh_tasks(self):
            tasks = self._get_tasks()
            self._task_ids = [t["id"] for t in tasks]
            tbl   = self.query_one("#task-table", DataTable)
            tbl.clear()

            imp_icons = {1: "[bold red]HIGH[/]", 2: "[yellow]MED [/]", 3: "[dim]LOW [/]"}

            # Backlog mode — group by subject with headers
            if self.view_mode == "backlog" and self.current_subject is None:
                grouped: dict = {}
                for t in tasks:
                    grouped.setdefault(t.get("subject_name") or "—", []).append(t)
                self._task_ids = []
                for subj, group in sorted(grouped.items()):
                    for t in group:
                        self._task_ids.append(t["id"])
                        self._add_task_row(tbl, t, imp_icons)
            else:
                for t in tasks:
                    self._add_task_row(tbl, t, imp_icons)

            label = VIEW_LABELS.get(self.view_mode, self.view_mode)
            self.query_one("#filter-bar", Static).update(
                f" {label} — {len(tasks)} task{'s' if len(tasks)!=1 else ''}"
                f"  [dim]a:add  e:edit  space:status  P:pin  L:someday  p:ping  1-7:view[/]"
            )

        def _add_task_row(self, tbl, t, imp_icons):
            done  = t["status"] == "done"
            over  = is_overdue(t.get("due_at"), t["status"])
            pin   = "📌" if t["pinned"] else ""
            st_ic = STATUS_ICON.get(t["status"], "○")
            title = f"[strike dim]{t['title']}[/]" if done else t["title"]
            subj  = (t.get("subject_name") or "")[:13]
            due   = format_due(t.get("due_at"), short=True)
            due_r = f"[red]{due}[/]" if over else due
            hrs   = f"{t['est_hours']:.1f}h" if t.get("est_hours") else ""
            score = f"{priority_score(t['importance'], t.get('due_at')):.1f}"
            wb    = work_by(t.get("due_at"), t.get("est_hours")) or ""
            tbl.add_row(st_ic, imp_icons.get(t["importance"],""), pin,
                        title, subj, due_r, hrs, score, wb)

        # ── Sidebar events ────────────────────────────────────────────────────

        def on_list_view_selected(self, event: ListView.Selected) -> None:
            sid = event.item.id or ""
            if sid.startswith("view-"):
                self.view_mode       = sid[5:]
                self.current_subject = None
            elif sid == "subj-all":
                self.current_subject = None
            elif sid.startswith("subj-"):
                self.current_subject = int(sid[5:])
            self.refresh_tasks()

        # ── Actions ───────────────────────────────────────────────────────────

        async def action_add_task(self) -> None:
            result = await self.push_screen_wait(TaskModal(subjects=self._get_subjects()))
            if result:
                c = get_db()
                c.execute("""
                    INSERT INTO tasks (subject_id,title,notes,due_at,recur,importance,
                                       status,est_hours,kind)
                    VALUES (?,?,?,?,?,?,?,?,?)
                """, (result["subject_id"], result["title"], result["notes"],
                      result["due_at"], result.get("recur"), result["importance"],
                      result["status"], result["est_hours"],
                      result.get("kind", "deadline")))
                c.commit(); c.close()
                self.refresh_tasks()

        async def action_edit_task(self) -> None:
            tbl = self.query_one("#task-table", DataTable)
            idx = tbl.cursor_row
            if idx < 0 or idx >= len(self._task_ids): return
            tid  = self._task_ids[idx]
            c    = get_db()
            task = dict(c.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone() or {})
            c.close()
            if not task: return
            result = await self.push_screen_wait(
                TaskModal(task=task, subjects=self._get_subjects()))
            if result:
                c = get_db()
                c.execute("""
                    UPDATE tasks SET subject_id=?,title=?,notes=?,due_at=?,recur=?,
                    importance=?,status=?,est_hours=?,kind=?,
                    done_at=CASE WHEN ?='done' AND status!='done'
                                 THEN ? ELSE done_at END
                    WHERE id=?
                """, (result["subject_id"], result["title"], result["notes"],
                      result["due_at"], result.get("recur"), result["importance"],
                      result["status"], result["est_hours"],
                      result.get("kind", "deadline"),
                      result["status"], datetime.now().isoformat(), tid))
                c.commit(); c.close()
                self.refresh_tasks()

        def action_cycle_status(self) -> None:
            tbl = self.query_one("#task-table", DataTable)
            idx = tbl.cursor_row
            if idx < 0 or idx >= len(self._task_ids): return
            tid = self._task_ids[idx]
            c   = get_db()
            task = c.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
            if not task: c.close(); return
            new_st  = STATUS_NEXT[task["status"]]
            done_at = datetime.now().isoformat() if new_st == "done" else None
            c.execute("UPDATE tasks SET status=?, done_at=? WHERE id=?",
                      (new_st, done_at, tid))
            if new_st == "done":
                _spawn_next_recurrence(c, dict(task))
            c.commit(); c.close()
            self.refresh_tasks()

        def action_delete_task(self) -> None:
            tbl = self.query_one("#task-table", DataTable)
            idx = tbl.cursor_row
            if idx < 0 or idx >= len(self._task_ids): return
            tid = self._task_ids[idx]
            c   = get_db()
            c.execute("DELETE FROM tasks WHERE id=?", (tid,))
            c.commit(); c.close()
            self._task_ids.pop(idx)
            self.refresh_tasks()

        def action_toggle_pin(self) -> None:
            tbl = self.query_one("#task-table", DataTable)
            idx = tbl.cursor_row
            if idx < 0 or idx >= len(self._task_ids): return
            tid = self._task_ids[idx]
            c   = get_db()
            task = c.execute("SELECT pinned FROM tasks WHERE id=?", (tid,)).fetchone()
            if task:
                c.execute("UPDATE tasks SET pinned=? WHERE id=?",
                          (0 if task["pinned"] else 1, tid))
                c.commit()
            c.close(); self.refresh_tasks()

        def action_toggle_someday(self) -> None:
            """Toggle task kind between someday and deadline."""
            tbl = self.query_one("#task-table", DataTable)
            idx = tbl.cursor_row
            if idx < 0 or idx >= len(self._task_ids): return
            tid = self._task_ids[idx]
            c   = get_db()
            task = c.execute("SELECT kind FROM tasks WHERE id=?", (tid,)).fetchone()
            if task:
                new_kind = "deadline" if task["kind"] == "someday" else "someday"
                c.execute("UPDATE tasks SET kind=? WHERE id=?", (new_kind, tid))
                c.commit()
            c.close(); self.refresh_tasks()

        async def action_new_subject(self) -> None:
            name = await self.push_screen_wait(SubjectModal())
            if name:
                c = get_db()
                try:
                    c.execute("INSERT INTO subjects (name) VALUES (?)", (name,))
                    c.commit()
                except sqlite3.IntegrityError:
                    pass
                c.close()
                self.refresh_subjects()

        def action_ping_task(self) -> None:
            tbl = self.query_one("#task-table", DataTable)
            idx = tbl.cursor_row
            if idx < 0 or idx >= len(self._task_ids): return
            tid = self._task_ids[idx]
            c   = get_db()
            task = c.execute("SELECT title,due_at FROM tasks WHERE id=?", (tid,)).fetchone()
            c.close()
            if task:
                notify(f"📌 {task['title']}", format_due(task["due_at"]))
                self.notify(f"Pinged: {task['title']}")

        def action_set_view(self, mode: str) -> None:
            self.view_mode       = mode
            self.current_subject = None
            self.refresh_tasks()

        def action_refresh(self) -> None:
            self.refresh_tasks()


# ─── Desktop Widget ────────────────────────────────────────────────────────────

def run_widget():
    try:
        import tkinter as tk
        from tkinter import font as tkfont
    except ImportError:
        print("tkinter not available — install python3-tk", file=sys.stderr)
        sys.exit(1)

    init_db()

    BG         = "#1c1c2e"
    BG2        = "#252540"
    BG3        = "#1a1a30"   # pinned section bg
    FG         = "#e0e0f0"
    FG_DIM     = "#7070a0"
    FG_DONE    = "#404060"
    ACCENT     = "#7c5cbf"
    RED        = "#e05c6e"
    YELLOW     = "#e0b84a"
    GREEN      = "#4abf8a"
    ORANGE     = "#e08a4a"
    BORDER     = "#3a3a60"
    FONT_FAM   = None  # resolved lazily in __init__ after root window exists

    WIDGET_W   = 320
    WIDGET_H   = 460
    REFRESH_MS = 30_000

    STATUS_COLOR = {"todo": FG_DIM, "wip": ORANGE, "done": GREEN}
    IMP_COLOR    = {1: RED, 2: YELLOW, 3: FG_DIM}

    class TaskWidget(tk.Tk):
        def __init__(self):
            super().__init__()
            # Resolve font family now that a root window exists
            nonlocal FONT_FAM
            if FONT_FAM is None:
                FONT_FAM = next(
                    (f for f in ("JetBrains Mono", "Monospace", "monospace")
                     if f in tkfont.families()),
                    "monospace"
                )
            self._drag_x = self._drag_y = 0
            self._pinned = True
            self._setup_window()
            self._build_ui()
            self._load_tasks()
            self._schedule_refresh()

        def _setup_window(self):
            self.title("taskman")
            self.geometry(f"{WIDGET_W}x{WIDGET_H}+40+80")
            self.overrideredirect(True)
            self.configure(bg=BG)
            self.attributes("-topmost", True)
            self.attributes("-alpha", 0.95)
            self.resizable(False, False)

        def _build_ui(self):
            f  = tkfont.Font(family=FONT_FAM, size=9)
            fb = tkfont.Font(family=FONT_FAM, size=9, weight="bold")
            fs = tkfont.Font(family=FONT_FAM, size=8)

            # Title bar
            bar = tk.Frame(self, bg=ACCENT, cursor="fleur")
            bar.pack(fill="x")
            tk.Label(bar, text="  ☰ taskman", bg=ACCENT, fg="white",
                     font=fb, anchor="w").pack(side="left", fill="x", expand=True)
            tk.Label(bar, text=" ✕ ", bg=ACCENT, fg="white",
                     font=fb, cursor="hand2").pack(side="right").bind(
                "<Button-1>", lambda _: self.destroy())
            pin_lbl = tk.Label(bar, text="📌", bg=ACCENT, fg="white",
                               font=fb, cursor="hand2", padx=3)
            pin_lbl.pack(side="right")
            pin_lbl.bind("<Button-1>", self._toggle_pin)
            self._pin_lbl = pin_lbl
            bar.bind("<ButtonPress-1>", self._drag_start)
            bar.bind("<B1-Motion>",     self._drag_move)
            for c in bar.winfo_children():
                c.bind("<ButtonPress-1>", self._drag_start)
                c.bind("<B1-Motion>",     self._drag_move)

            # Stats bar
            self._stats = tk.Label(self, bg=BG2, fg=FG_DIM, font=fs,
                                   anchor="w", padx=6, pady=2)
            self._stats.pack(fill="x")

            # Score / work-by toggle label
            self._meta_lbl = tk.Label(self, bg=BG2, fg=FG_DIM, font=fs,
                                      anchor="w", padx=6, pady=1)
            self._meta_lbl.pack(fill="x")

            # Canvas
            outer = tk.Frame(self, bg=BG)
            outer.pack(fill="both", expand=True, pady=(2,0))
            self._canvas = tk.Canvas(outer, bg=BG, highlightthickness=0, bd=0)
            vsb = tk.Scrollbar(outer, orient="vertical", command=self._canvas.yview)
            self._canvas.configure(yscrollcommand=vsb.set)
            vsb.pack(side="right", fill="y")
            self._canvas.pack(side="left", fill="both", expand=True)
            self._inner = tk.Frame(self._canvas, bg=BG)
            self._cwin  = self._canvas.create_window((0,0), window=self._inner, anchor="nw")
            self._inner.bind("<Configure>", lambda _: self._canvas.configure(
                scrollregion=self._canvas.bbox("all")))
            self._canvas.bind("<Configure>", lambda e: self._canvas.itemconfig(
                self._cwin, width=e.width))
            for seq in ("<MouseWheel>","<Button-4>","<Button-5>"):
                self._canvas.bind(seq, self._on_scroll)

            # Footer
            foot = tk.Frame(self, bg=BG2)
            foot.pack(fill="x", side="bottom")
            foot_row = tk.Frame(foot, bg=BG2); foot_row.pack(fill="x")
            tk.Label(foot_row, text="Click=status · R-click=menu · Dbl=TUI",
                     bg=BG2, fg=FG_DIM, font=fs, pady=2).pack(side="left", padx=4)

            self._f = f; self._fb = fb; self._fs = fs

        def _drag_start(self, e):
            self._drag_x = e.x_root - self.winfo_x()
            self._drag_y = e.y_root - self.winfo_y()
        def _drag_move(self, e):
            self.geometry(f"+{e.x_root-self._drag_x}+{e.y_root-self._drag_y}")
        def _toggle_pin(self, _=None):
            self._pinned = not self._pinned
            self.attributes("-topmost", self._pinned)
            self._pin_lbl.config(fg="white" if self._pinned else FG_DIM)
        def _on_scroll(self, e):
            if e.num==4 or e.delta>0: self._canvas.yview_scroll(-1,"units")
            elif e.num==5 or e.delta<0: self._canvas.yview_scroll(1,"units")

        def _fetch_tasks(self):
            c = get_db()
            rows = c.execute("""
                SELECT t.*, s.name AS subject_name FROM tasks t
                LEFT JOIN subjects s ON t.subject_id=s.id
                WHERE t.parent_id IS NULL
                ORDER BY t.pinned DESC, t.importance ASC, t.due_at ASC NULLS LAST
            """).fetchall()
            c.close()
            return [dict(r) for r in rows]

        def _load_tasks(self):
            tasks = self._fetch_tasks()
            for w in self._inner.winfo_children():
                w.destroy()

            now   = datetime.now()
            today = now.date()
            open_tasks = [t for t in tasks if t["status"]!="done"
                          and t.get("kind","deadline") not in ("someday","waiting")]

            pinned   = [t for t in open_tasks if t["pinned"]]
            overdue  = [t for t in open_tasks if not t["pinned"] and t.get("due_at")
                        and datetime.fromisoformat(t["due_at"]) < now]
            wip      = [t for t in open_tasks if not t["pinned"] and t["status"]=="wip"
                        and t not in overdue]
            due_today= [t for t in open_tasks if not t["pinned"] and t.get("due_at")
                        and datetime.fromisoformat(t["due_at"]).date()==today
                        and t not in overdue]
            upcoming = [t for t in open_tasks if not t["pinned"] and t not in overdue
                        and t not in wip and t not in due_today]
            done_today=[t for t in tasks if t["status"]=="done" and t.get("done_at")
                        and datetime.fromisoformat(t["done_at"]).date()==today][:5]

            # Stats
            total_open = len(open_tasks)
            wip_count  = len([t for t in open_tasks if t["status"]=="wip"])
            top_scores = sorted(open_tasks,
                key=lambda t: -priority_score(t["importance"], t.get("due_at")))[:1]
            top_score  = f"  top: {top_scores[0]['title'][:20]}" if top_scores else ""
            self._stats.config(
                text=f"  {len(overdue)} overdue  ·  {wip_count} wip  ·  "
                     f"{len(due_today)} today  ·  {total_open} open")
            self._meta_lbl.config(text=f"  {top_score}")

            def section(label, color, items, show_if_empty=False):
                if not items and not show_if_empty: return
                hdr = tk.Frame(self._inner, bg=BG2)
                hdr.pack(fill="x", pady=(5,0))
                tk.Label(hdr, text=f"  {label}", bg=BG2, fg=color,
                         font=self._fb, anchor="w", pady=2).pack(fill="x")
                for t in items:
                    self._task_row(t)

            if pinned:   section("📌  PINNED",      ACCENT, pinned)
            section("⚠  OVERDUE",        RED,    overdue)
            section("◑  IN PROGRESS",    ORANGE, wip)
            section("📅  TODAY",          YELLOW, due_today)
            section("🗂  UPCOMING",       ACCENT, upcoming, show_if_empty=True)
            if done_today: section("✓  DONE TODAY",  GREEN, done_today)
            self._canvas.yview_moveto(0)

        def _task_row(self, task):
            done   = task["status"] == "done"
            over   = is_overdue(task.get("due_at"), task["status"])
            row    = tk.Frame(self._inner, bg=BG, cursor="hand2")
            row.pack(fill="x", padx=4, pady=1)

            st_char  = STATUS_ICON.get(task["status"], "○")
            st_color = STATUS_COLOR.get(task["status"], FG_DIM)
            chk = tk.Label(row, text=st_char, bg=BG, fg=st_color,
                           font=self._fb, width=2)
            chk.pack(side="left")

            imp_color = IMP_COLOR.get(task["importance"], FG_DIM)
            imp_s     = {1:"H",2:"M",3:"L"}.get(task["importance"],"M")
            tk.Label(row, text=imp_s, bg=BG, fg=imp_color,
                     font=self._fs, width=1).pack(side="left")

            title    = task["title"]
            if len(title) > 30: title = title[:29] + "…"
            title_fg = FG_DONE if done else (RED if over else FG)
            title_lbl = tk.Label(row, text=title, bg=BG, fg=title_fg,
                                  font=self._f, anchor="w")
            title_lbl.pack(side="left", fill="x", expand=True)

            due_s = format_due(task.get("due_at"), short=True)
            if due_s:
                tk.Label(row, text=due_s, bg=BG,
                         fg=RED if over else FG_DIM,
                         font=self._fs, padx=3).pack(side="right")

            score_s = f"{priority_score(task['importance'],task.get('due_at')):.0f}pt"
            tk.Label(row, text=score_s, bg=BG, fg=FG_DIM,
                     font=self._fs, padx=2).pack(side="right")

            tk.Frame(self._inner, bg=BORDER, height=1).pack(fill="x", padx=8)

            tid = task["id"]

            def on_click(_, tid=tid):
                c    = get_db()
                task = c.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
                if not task: c.close(); return
                new_st  = STATUS_NEXT[task["status"]]
                done_at = datetime.now().isoformat() if new_st=="done" else None
                c.execute("UPDATE tasks SET status=?,done_at=? WHERE id=?",
                          (new_st, done_at, tid))
                if new_st=="done": _spawn_next_recurrence(c, dict(task))
                c.commit(); c.close()
                self._load_tasks()

            def on_right(e, tid=tid, title=task["title"]):
                menu = tk.Menu(self, tearoff=0, bg=BG2, fg=FG,
                               activebackground=ACCENT, activeforeground="white",
                               font=self._fs)
                menu.add_command(label=f"#{tid}: {title[:26]}", state="disabled")
                menu.add_separator()
                menu.add_command(label=f"  {STATUS_ICON['todo']} Not Started",
                                 command=lambda: self._set_status(tid, "todo"))
                menu.add_command(label=f"  {STATUS_ICON['wip']} In Progress",
                                 command=lambda: self._set_status(tid, "wip"))
                menu.add_command(label=f"  {STATUS_ICON['done']} Done",
                                 command=lambda: self._set_status(tid, "done"))
                menu.add_separator()
                menu.add_command(label="  📌  Toggle pin",
                                 command=lambda: self._toggle_task_pin(tid))
                menu.add_command(label="  🌙  Toggle someday",
                                 command=lambda: self._toggle_task_someday(tid))
                menu.add_command(label="  ⏰  Ping me",
                                 command=lambda: notify(f"📌 {title}",
                                     format_due(task.get("due_at"))))
                menu.add_separator()
                menu.add_command(label="  🗑  Delete",
                                 command=lambda: [
                                     get_db().execute("DELETE FROM tasks WHERE id=?", (tid,)),
                                     get_db().commit(),
                                     self._load_tasks()])
                menu.add_command(label="  ↗  Open TUI",
                                 command=lambda: subprocess.Popen(
                                     ["taskman"], stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL))
                try: menu.tk_popup(e.x_root, e.y_root)
                finally: menu.grab_release()

            for w in (row, chk, title_lbl):
                w.bind("<Button-1>",  on_click)
                w.bind("<Button-3>",  on_right)
                w.bind("<Double-1>",  lambda _: subprocess.Popen(
                    ["taskman"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))

        def _set_status(self, tid, status):
            c = get_db()
            task = c.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
            if task:
                done_at = datetime.now().isoformat() if status=="done" else None
                c.execute("UPDATE tasks SET status=?,done_at=? WHERE id=?",
                          (status, done_at, tid))
                if status=="done": _spawn_next_recurrence(c, dict(task))
                c.commit()
            c.close(); self._load_tasks()

        def _toggle_task_pin(self, tid):
            c = get_db()
            t = c.execute("SELECT pinned FROM tasks WHERE id=?", (tid,)).fetchone()
            if t:
                c.execute("UPDATE tasks SET pinned=? WHERE id=?",
                          (0 if t["pinned"] else 1, tid))
                c.commit()
            c.close(); self._load_tasks()

        def _toggle_task_someday(self, tid):
            c = get_db()
            t = c.execute("SELECT kind FROM tasks WHERE id=?", (tid,)).fetchone()
            if t:
                new_kind = "deadline" if t["kind"] == "someday" else "someday"
                c.execute("UPDATE tasks SET kind=? WHERE id=?", (new_kind, tid))
                c.commit()
            c.close(); self._load_tasks()

        def _schedule_refresh(self):
            self._load_tasks()
            self.after(REFRESH_MS, self._schedule_refresh)

    TaskWidget().mainloop()


# ─── Argument parser + entry point ─────────────────────────────────────────────

def cli_export(args):
    """Export open tasks to JSON for GitHub Actions sync."""
    with get_db() as conn:
        tasks = conn.execute("""
            SELECT t.id, t.title, t.notes, t.due_at, t.importance, t.status,
                   t.est_hours, t.pinned, t.kind, t.recur, t.created_at,
                   s.name AS subject
            FROM tasks t
            LEFT JOIN subjects s ON s.id = t.subject_id
            WHERE t.status != 'done' AND t.kind NOT IN ('someday','waiting')
        """).fetchall()
    out = [dict(r) for r in tasks]
    path = Path(args.output) if args.output else Path.cwd() / "tasks.json"
    path.write_text(json.dumps(out, indent=2, default=str))
    print(f"Exported {len(out)} tasks → {path}")


def cli_sync(args):
    """Pull from GitHub (importing phone-added tasks), then export+push tasks.json."""
    cfg = load_config()
    repo = args.repo or cfg.get("sync_repo", "")
    if not repo:
        print("No sync repo set. Run: taskman config --sync-repo /path/to/repo")
        return
    repo_path = Path(repo).expanduser()
    if not (repo_path / ".git").exists():
        print(f"Not a git repo: {repo_path}")
        return

    # 1. Pull latest from GitHub so we see incoming/ tasks added from phone
    print("Pulling from GitHub...")
    subprocess.run(["git", "-C", str(repo_path), "pull", "--rebase"], check=True)

    # 2. Import any incoming/*.json tasks into local SQLite
    incoming_dir = repo_path / "incoming"
    imported = []
    if incoming_dir.exists():
        importance_map = {"high": 1, "med": 2, "low": 3}
        with get_db() as conn:
            for f in sorted(incoming_dir.glob("*.json")):
                try:
                    task = json.loads(f.read_text())
                    # Resolve subject name → id
                    subject_id = None
                    subject_name = task.get("subject", "").strip()
                    if subject_name:
                        row = conn.execute(
                            "SELECT id FROM subjects WHERE name = ?", (subject_name,)
                        ).fetchone()
                        if row:
                            subject_id = row["id"]
                        else:
                            conn.execute(
                                "INSERT OR IGNORE INTO subjects (name) VALUES (?)",
                                (subject_name,))
                            subject_id = conn.execute(
                                "SELECT id FROM subjects WHERE name = ?",
                                (subject_name,)).fetchone()["id"]

                    imp = task.get("importance", 2)
                    if isinstance(imp, str):
                        imp = importance_map.get(imp.lower(), 2)

                    conn.execute("""
                        INSERT INTO tasks
                          (subject_id, title, notes, due_at, importance,
                           status, est_hours, pinned, kind, created_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?)
                    """, (
                        subject_id,
                        task.get("title", "Untitled"),
                        task.get("notes", ""),
                        task.get("due_at"),
                        imp,
                        task.get("status", "todo"),
                        task.get("est_hours"),
                        task.get("pinned", 0),
                        task.get("kind", "deadline"),
                        task.get("created_at", datetime.now().isoformat()),
                    ))
                    imported.append(task.get("title", f.name))
                    f.unlink()
                except Exception as e:
                    print(f"  Warning: could not import {f.name}: {e}")

    if imported:
        print(f"Imported {len(imported)} task(s) from phone:")
        for t in imported:
            print(f"  + {t}")
        # Commit removal of imported files
        subprocess.run(["git", "-C", str(repo_path), "add", "incoming/"], check=True)
        subprocess.run(["git", "-C", str(repo_path), "commit", "-m",
                        f"import: {len(imported)} task(s) from phone [skip ci]"],
                       check=True)

    # 3. Export current SQLite state to tasks.json
    export_path = repo_path / "tasks.json"
    class ExportArgs:
        output = str(export_path)
    cli_export(ExportArgs())

    # 4. Push everything
    result = subprocess.run(
        ["git", "-C", str(repo_path), "status", "--porcelain"],
        capture_output=True, text=True)
    if result.stdout.strip():
        subprocess.run(["git", "-C", str(repo_path), "add", "tasks.json"], check=True)
        subprocess.run(["git", "-C", str(repo_path), "commit", "-m",
                        f"sync: {datetime.now().strftime('%Y-%m-%d %H:%M')} [skip ci]"],
                       check=True)

    subprocess.run(["git", "-C", str(repo_path), "push"], check=True)
    print("Sync complete.")


def cli_config(args):
    cfg = load_config()
    changed = False
    if args.ntfy_topic is not None:
        cfg["ntfy_topic"] = args.ntfy_topic
        changed = True
    if args.ntfy_server is not None:
        cfg["ntfy_server"] = args.ntfy_server
        changed = True
    if args.sync_repo is not None:
        cfg["sync_repo"] = str(Path(args.sync_repo).expanduser().resolve())
        changed = True
    if changed:
        save_config(cfg)
        print("Config saved.")
    if args.test:
        topic = cfg.get("ntfy_topic", "").strip()
        if not topic:
            print("No ntfy topic set. Run: taskman config --ntfy-topic YOUR_TOPIC")
            return
        notify("taskman test", "If you see this on your phone, ntfy is working! ✓", "normal")
        print(f"Test notification sent to ntfy topic: {topic}")
        return
    # Print current config
    print(f"ntfy_topic  : {cfg.get('ntfy_topic', '(not set)')}")
    print(f"ntfy_server : {cfg.get('ntfy_server', 'https://ntfy.sh (default)')}")
    if not cfg.get("ntfy_topic"):
        print("\nTo enable phone notifications:")
        print("  taskman config --ntfy-topic YOUR_TOPIC")
        print("  taskman config --test")


def build_parser():
    p   = argparse.ArgumentParser(prog="taskman",
            description="Terminal task manager — Notion Planner, natively.")
    sub = p.add_subparsers(dest="cmd")

    # add
    pa = sub.add_parser("add", help="Add a task")
    pa.add_argument("title")
    pa.add_argument("--subject",    "-S", default=None,
                    help="Subject / category (Personal, Career, MATH2460, …)")
    pa.add_argument("--importance", "-i", default="med",
                    choices=["high","h","med","m","low","l"])
    pa.add_argument("--due",        "-d", default=None, metavar="DATETIME",
                    help="Due date as ISO string, e.g. '2026-09-22 23:59'")
    pa.add_argument("--date",       default=None, metavar="MMDDYYYY",
                    help="Due date in MMDDYYYY format, e.g. 09242026")
    pa.add_argument("--time",       default=None, metavar="HHMMa|HHMMp",
                    help="Due time, e.g. 1159p for 11:59pm (use with --date)")
    pa.add_argument("--hours",      "-H", type=float, default=None,
                    metavar="N", help="Estimated hours")
    pa.add_argument("--kind",       "-k", default="deadline",
                    choices=list(KINDS),
                    help="Task kind (deadline/habit/project/quick/waiting/someday)")
    pa.add_argument("--notes",      "-n", default=None)
    pa.add_argument("--pin",        action="store_true", help="Pin to top")

    # list (open tasks, sorted by priority score)
    pl = sub.add_parser("list", aliases=["ls"], help="List tasks")
    pl.add_argument("n", nargs="?", type=int, default=5,
                    metavar="N", help="Number of tasks to show (default 5)")
    pl.add_argument("--subject",    "-S", default=None)
    pl.add_argument("--all",        "-a", action="store_true")

    # backlog (grouped by subject)
    sub.add_parser("backlog", help="Backlog grouped by subject")

    # done-today
    sub.add_parser("done-today", help="Tasks completed today")

    # someday
    sub.add_parser("someday", help="Show Someday / Maybe tasks")
    # lot kept as legacy alias
    sub.add_parser("lot", help="Show Someday tasks (alias for someday)")

    # status
    ped = sub.add_parser("edit", help="Interactively edit a task")
    ped.add_argument("id", type=int, help="Task ID to edit")

    ps = sub.add_parser("status", help="Set task status")
    ps.add_argument("id", type=int)
    ps.add_argument("new_status", nargs="?",
                    choices=["todo","wip","done"],
                    help="Omit to cycle to next status")

    # rm
    pr = sub.add_parser("rm", aliases=["remove","delete"], help="Delete a task")
    pr.add_argument("id", type=int)

    # ping
    pp = sub.add_parser("ping", help="Send desktop notification for a task")
    pp.add_argument("id", type=int)

    # subjects
    sub.add_parser("subjects", help="List subjects with open task counts")

    # daemon
    sub.add_parser("daemon", help="Run notification daemon in foreground")

    # widget
    sub.add_parser("widget", help="Launch floating desktop widget")

    # tui
    sub.add_parser("tui", help="Open the TUI (default)")

    # export
    pe = sub.add_parser("export", help="Export open tasks to tasks.json")
    pe.add_argument("--output", "-o", default=None, metavar="PATH",
                    help="Output path (default: ./tasks.json)")

    # sync
    ps2 = sub.add_parser("sync", help="Export + git push tasks.json to sync repo")
    ps2.add_argument("--repo", default=None, metavar="PATH",
                     help="Path to git repo (overrides config sync_repo)")

    # config
    pc = sub.add_parser("config", help="View or set configuration")
    pc.add_argument("--ntfy-topic", metavar="TOPIC",
                    help="ntfy.sh topic name for phone notifications (e.g. abhiram-taskman)")
    pc.add_argument("--ntfy-server", metavar="URL", default=None,
                    help="ntfy server URL (default: https://ntfy.sh)")
    pc.add_argument("--sync-repo", metavar="PATH", default=None,
                    help="Path to local git repo for GitHub Actions sync")
    pc.add_argument("--test", action="store_true",
                    help="Send a test notification to your phone")

    return p

def main():
    parser = build_parser()
    args   = parser.parse_args()

    if args.cmd in (None, "tui"):
        if not HAS_TEXTUAL:
            print("TUI requires textual: pip install textual", file=sys.stderr)
            sys.exit(1)
        TaskManApp().run()
    elif args.cmd == "add":                        cli_add(args)
    elif args.cmd in ("list", "ls"):
        args.someday = False
        cli_list(args)
    elif args.cmd == "backlog":                    cli_list_backlog(args)
    elif args.cmd == "done-today":
        args.subject = None; args.done_today = True; args.all = False; args.someday = False
        cli_list(args)
    elif args.cmd in ("someday", "lot"):
        args.subject = None; args.done_today = False; args.all = False; args.someday = True; args.n = None
        cli_list(args)
    elif args.cmd == "edit":                        cli_edit(args)
    elif args.cmd == "status":                     cli_status(args)
    elif args.cmd in ("rm","remove","delete"):     cli_rm(args)
    elif args.cmd == "ping":                       cli_ping(args)
    elif args.cmd == "subjects":                   cli_subjects(args)
    elif args.cmd == "daemon":                     run_daemon_foreground()
    elif args.cmd == "widget":                     run_widget()
    elif args.cmd == "export":                     cli_export(args)
    elif args.cmd == "sync":                       cli_sync(args)
    elif args.cmd == "config":                     cli_config(args)

if __name__ == "__main__":
    main()
