# taskman — Full Background & Context Document

> **Purpose of this file**: Give a fresh LLM complete context to understand, extend, debug, or recreate the `taskman` system from scratch. Written as of September 2026. Do not summarize; read every section.

---

## 1. Who is the user

**Name**: Abhiram Kuuram  
**Email**: claudeumd@gmail.com  
**University**: University of Maryland (UMD), College Park MD  
**Role**: DevSecOps engineer (has at least one direct report) + full-time student  
**Background**: AWS (EKS, Karpenter, Terraform, CloudFormation, S3/CloudFront), Kubernetes tooling (Prometheus, Grafana, Datadog, OpenCost, Goldilocks, VPA, KEDA), security tooling (Semgrep, Trivy, CodeQL)  
**Phone**: iPhone  
**Laptop OS**: Pop!_OS (Ubuntu-based), GNOME desktop, **Wayland** compositor  
**Display**: 2880×1800 @ **200% HiDPI scale**, Fractional Scaling ON, HiDPI Daemon OFF  
**Shell**: bash/zsh, `~/.local/bin` on PATH  
**Active subjects (categories)**: Personal, Career, MATH2460, cmsc351  

---

## 2. What taskman is and why it was built

`taskman` is a **personal task manager** modeled after the user's Notion Planner setup. The user previously used Notion's planner view with properties like importance (HIGH/MED/LOW), due date, subject/area, status (todo/wip/done), estimated hours, and a computed priority score. The goal was to replicate that workflow entirely in the terminal — faster, offline-capable, and integrated with the desktop — without Notion's overhead.

The system has **three interfaces** that share a single SQLite database:

1. **CLI** — quick one-liners from any terminal (`taskman add "Buy milk"`)
2. **TUI** — full interactive terminal UI built with [Textual](https://github.com/Textualize/textual), launched by running `taskman` with no arguments
3. **Widget** — a small frameless floating `tkinter` window that stays on the desktop, showing the current task list at a glance

Additionally:
- A **background daemon** runs as a systemd user service, firing desktop notifications and ntfy push notifications for due/overdue tasks and a morning digest
- A **GitHub Actions sync** allows tasks to be submitted from the iPhone (via iOS Shortcut → GitHub API) and automatically pulled into the local DB every 15 minutes

---

## 3. File locations

| Path | Purpose |
|------|---------|
| `~/.local/bin/taskman` | The single Python script — CLI + TUI + widget + daemon, everything in one file |
| `~/.local/share/taskman/tasks.db` | SQLite database (WAL mode, foreign keys ON) |
| `~/.local/share/taskman/config.json` | JSON config file (ntfy settings, sync repo path, last digest date) |
| `~/.config/systemd/user/taskman-daemon.service` | Systemd user service for the background daemon |
| `~/.config/autostart/taskman-widget.desktop` | GNOME autostart entry — launches widget on login |

The script is installed by `install.sh` (in the same directory as `taskman.py`). The install script:
1. Installs `textual` via pip (`--break-system-packages`)
2. Copies `taskman.py` → `~/.local/bin/taskman` and `chmod +x`
3. Adds `~/.local/bin` to PATH in `.bashrc`/`.zshrc` if missing
4. Creates and enables `taskman-daemon.service` via systemctl --user
5. Creates the autostart `.desktop` entry for the widget

---

## 4. Database schema

```sql
CREATE TABLE subjects (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL UNIQUE,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE tasks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_id   INTEGER REFERENCES subjects(id) ON DELETE SET NULL,
    parent_id    INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
    title        TEXT NOT NULL,
    notes        TEXT DEFAULT '',
    due_at       TEXT,           -- ISO datetime string e.g. "2026-09-22 23:59"
    recur        TEXT,           -- NULL | 'daily' | 'weekly' | 'monthly'
    importance   INTEGER DEFAULT 2,  -- 1=HIGH  2=MED  3=LOW
    status       TEXT DEFAULT 'todo',  -- 'todo' | 'wip' | 'done'
    done_at      TEXT,           -- ISO datetime when marked done
    est_hours    REAL,           -- estimated hours (used for "work by" calculation)
    pinned       INTEGER DEFAULT 0,   -- pinned tasks always sort to top
    parking_lot  INTEGER DEFAULT 0,   -- hidden from main views, separate "someday" bucket
    notified_due INTEGER DEFAULT 0,   -- 1 after due/overdue notification fired
    created_at   TEXT DEFAULT (datetime('now'))
);
```

Default subjects seeded at `init_db()`: Personal, Career, MATH2460, cmsc351.

**Priority Score formula** (mirrors original Notion formula):
```
imp_weight = {1: 3.0, 2: 2.0, 3: 1.0}[importance]
urgency    = 1 / max(days_left, 0.25)   # overdue → 8.0, no due date → 0.5
score      = imp_weight * urgency
```

**Status cycle**: todo → wip → done → todo (single click in widget or TUI)

**Recurrence**: when a recurring task is marked done, `_spawn_next_recurrence()` immediately creates a new task with the same fields and advances `due_at` by the recur interval.

---

## 5. Config file (`config.json`)

```json
{
  "ntfy_topic": "your-ntfy-topic-name",
  "ntfy_server": "https://ntfy.sh",
  "sync_repo": "/home/abhi/path/to/taskman-github-repo",
  "_last_digest_date": "2026-09-18"
}
```

- `ntfy_topic`: the topic name subscribed to in the ntfy iOS app
- `ntfy_server`: defaults to `https://ntfy.sh`; override for self-hosted
- `sync_repo`: absolute path to the local git clone of the GitHub sync repo
- `_last_digest_date`: persisted so the daemon doesn't double-send the morning digest across restarts

Configure via: `taskman config --ntfy-topic NAME --ntfy-server URL --sync-repo /path --test`

`--test` fires a test ntfy notification immediately.

---

## 6. CLI reference

```
taskman add "Title" [--subject SUBJECT] [--importance h|m|l]
                    [--date MMDDYYYY] [--time HHMMa|HHMMp]
                    [--due "ISO string fallback"]
                    [--hours N] [--recur daily|weekly|monthly]
                    [--pin] [--lot]
taskman list        [--subject NAME] [--backlog] [--lot] [--done-today]
taskman status <id> [todo|wip|done]
taskman done <id>
taskman rm <id>
taskman ping <id>           # fire a test notification for a specific task right now
taskman subjects            # list subjects
taskman daemon              # run notification daemon in foreground
taskman export              # dump open tasks to tasks.json (for sync)
taskman sync [--repo PATH]  # manual: pull→import incoming→export→push
taskman config [--ntfy-topic X] [--ntfy-server X] [--sync-repo X] [--test]
taskman widget              # launch the floating desktop widget
```

**Date/time parsing**: `--date 09222026 --time 1159p` → `"2026-09-22 23:59"`. Date is MMDDYYYY. Time is HHMM followed by `a` (AM) or `p` (PM). If only `--date` is given, defaults to 23:59 that day. ISO string fallback: `--due "2026-09-22 23:59"`.

---

## 7. TUI (Textual)

Launched by `taskman` with no arguments. Built with the [Textual](https://textual.textualize.io/) framework. Provides:
- Full task list with keyboard navigation
- Inline status cycling, pin toggle, parking lot toggle
- Add/edit task forms
- Subject management
- Filtered views (by subject, backlog, parking lot, done today)

The TUI and widget share the same SQLite DB — changes in one appear in the other on next refresh.

---

## 8. Floating widget

### Launch
```bash
taskman widget &
```
Auto-launched on GNOME login via `~/.config/autostart/taskman-widget.desktop`.

### Design philosophy
Minimal, always-visible, non-intrusive. Modeled after the HTML mockup (`widget-mockup.html` in the setup repo). No window decorations — uses `overrideredirect(True)` for a completely frameless window. Semi-transparent (alpha 0.78) so the desktop is visible through it.

### Window properties
- **Size**: 250×440 logical pixels
- **Position**: configured by user dragging (no saved position yet — starts at +40+80)
- **Always on top**: yes by default (toggleable via right-click menu on stats bar)
- **Alpha**: 0.78 (semi-transparent)
- **No title bar**: removed entirely; the stats bar serves as the header
- **Rounded corners**: GNOME Wayland compositor applies these automatically; no code needed

### HiDPI fix (Wayland-specific)
The laptop runs Wayland at 200% scale. tkinter runs through XWayland and renders at logical resolution; the compositor upscales 2× causing blur. Fix applied in `__init__`:
```python
if _ON_WAYLAND:  # detected via os.environ.get("WAYLAND_DISPLAY")
    self.tk.call("tk", "scaling", 96 / 72 * 2)  # ≈ 2.667 px/pt
```
This makes tkinter render fonts at 2× physical pixel density so the compositor upscales crisp pixels instead of blurry ones.

### Stats bar (top of widget)
Single line: `N open · N due soon · N wip`  
Acts as the header:
- **Drag**: click + drag to move window
- **Double-click**: fold/unfold (collapse to stats bar height only)
- **Right-click**: menu with Pin/Unpin toggle and Close

### Fold behavior
- Folded: body hidden, window shrinks to stats bar height only
- Unfolded: body restored, window returns to full 250×440 height
- No persistent fold state (always starts expanded)

### Task list sections (in order)
1. **PINNED** — tasks with `pinned=1`, regardless of status
2. **OVERDUE** — `due_at < now` and not done, not pinned
3. **IN PROGRESS** — `status='wip'`, not pinned, not overdue
4. **TODAY** — `due_at` date == today, not pinned, not overdue
5. **UPCOMING** — everything else that's open (always shown even if empty)
6. **DONE TODAY** — `status='done'` and `done_at` date == today, max 5 shown

Section headers: small-caps, `#7c5cbf` accent purple, 1px separator above.

### Task row layout
Each row uses a 3-column grid: `[3px accent bar] [● dot] [title+subject] [due date]`

**Left accent bar** (3px tall strip, full height of row):
- Purple (`#7c5cbf`) if pinned
- Red (`#e05c6e`) if overdue
- Orange (`#e08a4a`) if wip
- Transparent (BG color) otherwise

**Importance dot** (colored ●):
- HIGH (1): red `#e05c6e`
- MED (2): yellow `#e0b84a`
- LOW (3): green `#4abf8a`

**Title**: truncated at 20 chars with ellipsis if longer. Dimmed (`#404060`) if done.

**Subject** (below title, smaller font): `#7070a0` dim gray.

**Due date** (right-aligned):
- "OVRD" in red if overdue
- Time remaining if < 24h: "Xm" or "Xh" in yellow
- "Mon DD" in cyan if > 2 days away
- "—" in dim color if no due date
- "done" in dim if status=done

### Click interactions
- **Single click**: cycle status (todo → wip → done → todo)
- **Right-click**: context menu (set status directly, toggle pin, toggle parking lot, ping notification, delete, open TUI)
- **Double-click**: open TUI

### Color palette
```python
BG         = "#1c1c2e"   # main background
BG2        = "#252540"   # stats bar background
FG         = "#e0e0f0"   # primary text
FG_DIM     = "#7070a0"   # secondary text / subject names
FG_DONE    = "#404060"   # done task text
ACCENT     = "#7c5cbf"   # purple — pinned bar, section headers, title bar
RED        = "#e05c6e"   # HIGH importance, overdue
YELLOW     = "#e0b84a"   # MED importance, due < 48h
GREEN      = "#4abf8a"   # LOW importance, done
ORANGE     = "#e08a4a"   # wip accent bar
BORDER     = "#3a3a60"   # divider lines
CYAN       = "#5a9abf"   # normal due dates (> 2 days)
```

### Fonts
Family: JetBrains Mono (falls back to Monospace). Four sizes:
- `f`: size 9 — task titles
- `fb`: size 9 bold — (used in old title bar, kept for menus)
- `fs`: size 8 — due date labels, dot labels
- `fsc`: size 7 bold — section headers, stats bar, context menus (small-caps feel)

### Refresh
Widget auto-refreshes every 30 seconds (`REFRESH_MS = 30_000`). Also refreshes after any status change.

### Scrolling
Canvas + inner Frame. Scrollbar exists but is hidden — mousewheel scrolls via `<Button-4>`/`<Button-5>` bindings.

---

## 9. Notification daemon

### What it is
A `threading.Thread` subclass (`NotificationDaemon`) that runs inside the process launched by `taskman daemon`. Managed as a systemd user service so it persists even when the TUI/widget is closed.

### Systemd service
```ini
[Unit]
Description=taskman notification daemon
After=graphical-session.target

[Service]
Type=simple
ExecStart=/home/USER/.local/bin/taskman daemon
Restart=on-failure
RestartSec=10s
Environment=DISPLAY=:0
Environment=DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/%i/bus

[Install]
WantedBy=default.target
```

### Loop behavior
Runs `while not self._stop.wait(60)` — wakes every 60 seconds and calls:
1. `_check_due()` — fires notifications for overdue and due-soon tasks
2. `_check_digest()` — fires morning digest if past 9am and not yet sent today
3. `_maybe_sync()` — pulls/pushes to GitHub repo if configured and 15 min have elapsed

### Due notifications
- **Overdue**: fires once per task when `due_at < now` and `notified_due=0`. Sets `notified_due=1` in DB. Priority: critical.
- **Due soon**: fires once per task when `now <= due_at <= now + 15min` and `notified_due=0`. Priority: normal.

### Morning digest
- Fires when `datetime.now().hour >= 9` and today's date hasn't been digested yet
- Reports: N due today · N overdue · N total open
- `_last_digest` persisted to `config.json` as `_last_digest_date` (ISO date string) so daemon restart doesn't re-fire the digest
- **Key behavior**: if the laptop was off at 9am, the digest fires the moment the daemon starts later that day, as long as it's still the same day and past 9am

### Notification delivery
Two channels:
1. **Desktop** (Linux): `notify-send` via subprocess — appears as a GNOME notification bubble
2. **Phone** (ntfy.sh): HTTP POST to `{ntfy_server}/{ntfy_topic}` with `Title`, `Priority`, `Tags` headers

ntfy priority mapping: `low → "low"`, `normal → "default"`, `critical → "urgent"`

---

## 10. GitHub Actions sync (phone → laptop)

### Purpose
Allows the user to add tasks from their iPhone without any direct connection to the laptop. The phone triggers a GitHub Action, which commits a JSON task file to the repo. The laptop daemon pulls it in every 15 minutes.

### Repo structure
```
taskman-sync-repo/
├── .github/
│   └── workflows/
│       ├── add-task.yml       # triggered from phone to add a task
│       └── notify.yml         # optional: GitHub-side notifications (separate from daemon)
├── incoming/                  # phone-submitted task JSON files land here
│   └── <run_id>.json
├── tasks.json                 # exported snapshot of all open tasks (updated by sync)
└── notified.json              # tracks which tasks have been ntfy-notified (for notify.yml)
```

### `add-task.yml` workflow
Triggered via `workflow_dispatch` (manual trigger — can be called from iOS Shortcut via GitHub API).

Inputs:
- `title` (required): task title string
- `subject` (optional, default "Personal"): one of Personal/Career/MATH2460/cmsc351
- `due` (optional): due date/time as `YYYY-MM-DD HH:MM` or `YYYY-MM-DD`
- `importance` (optional, default "med"): choice of high/med/low
- `hours` (optional): estimated hours as a float string
- `notes` (optional): freeform notes string

The workflow:
1. Checks out the repo
2. Runs inline Python to build a task JSON object and write it to `incoming/<run_id>.json`
3. Commits and pushes the file (`git config user.name "taskman-bot"`)
4. Optionally sends an ntfy confirmation if `NTFY_TOPIC` secret is set

The JSON written to `incoming/`:
```json
{
  "title": "...",
  "subject": "Personal",
  "due_at": "2026-09-22 23:59" or null,
  "importance": 2,
  "est_hours": null or float,
  "notes": "",
  "status": "todo",
  "pinned": 0,
  "parking_lot": 0,
  "created_at": "2026-09-18T01:04:00",
  "source": "phone"
}
```
`importance` is stored as integer (1/2/3) in the JSON, not string.

### Secrets needed in the GitHub repo
- `NTFY_TOPIC`: your ntfy topic name (optional, enables confirmation push)
- `NTFY_SERVER`: your ntfy server URL (optional, defaults to ntfy.sh)
- A GitHub Personal Access Token (PAT) with `workflow` scope — needed only for iOS Shortcut to trigger the action via GitHub API

### iOS Shortcut (not yet set up as of writing)
Would use the Shortcut "Get Contents of URL" action to POST to:
```
https://api.github.com/repos/{owner}/{repo}/actions/workflows/add-task.yml/dispatches
```
with headers `Authorization: Bearer {PAT}` and `Accept: application/vnd.github+json`, and body:
```json
{
  "ref": "main",
  "inputs": {
    "title": "...",
    "subject": "Personal",
    "importance": "med"
  }
}
```

---

## 11. Auto-sync (laptop daemon ↔ GitHub)

The `NotificationDaemon._maybe_sync()` method runs every 15 minutes. If `sync_repo` is set in config:

1. `git pull --rebase --quiet` — fetch incoming tasks from GitHub
2. Read all `incoming/*.json` files:
   - Resolve subject name to `subject_id` (creates subject if new)
   - Insert task into local SQLite DB
   - Delete the `.json` file
3. If any tasks were imported:
   - `git add incoming/ && git commit -m "import: N task(s) [skip ci]"`
   - Send ntfy notification listing imported task titles
4. Export current open tasks from SQLite to `tasks.json` in the repo
5. If `tasks.json` changed: `git add tasks.json && git commit -m "sync: YYYY-MM-DD HH:MM [skip ci]"`
6. `git push --quiet`
7. Update `self._last_sync` timestamp

All errors are caught silently — the daemon never crashes over a sync failure.

The `[skip ci]` in commit messages prevents the `notify.yml` workflow from triggering on sync commits.

---

## 12. Manual sync command

`taskman sync [--repo PATH]` does the same as `_maybe_sync()` but synchronously, with print output, for manual use or troubleshooting.

---

## 13. ntfy configuration

ntfy.sh is a free/self-hostable push notification service. The user subscribes to a topic in the ntfy iOS app.

**Setup**:
```bash
taskman config --ntfy-topic your-unique-topic-name
taskman config --test   # verify it works
```

**iOS DND workaround**: ntfy on iOS cannot bypass system DND programmatically (Apple restriction). User must either:
- Settings → Focus → Do Not Disturb → Apps → add ntfy to "Always Allow"
- Settings → Notifications → ntfy → enable "Time Sensitive" (and use `max` priority for critical tasks)

---

## 14. `tasks.json` format (exported snapshot)

Written to the sync repo by `cli_export()` / `_maybe_sync()`. Contains only open, non-parking-lot tasks:
```json
[
  {
    "id": 12,
    "title": "Study for 351 exam",
    "notes": "",
    "due_at": "2026-09-22 23:59",
    "importance": 1,
    "status": "todo",
    "est_hours": 4.0,
    "pinned": 1,
    "parking_lot": 0,
    "recur": null,
    "created_at": "2026-09-10T22:00:00",
    "subject": "cmsc351"
  }
]
```
This file is intended to be read by a future phone-side web app to display tasks on the iPhone.

---

## 15. Planned but not yet implemented

- **iOS web view of tasks**: a simple HTML page (hosted as a GitHub Pages or Cloudflare Worker or artifact) that fetches `tasks.json` from the public/raw GitHub URL and renders a mobile-friendly task list. Would replace the "browse raw JSON" approach.
- **iOS Shortcut for adding tasks**: the GitHub API call from Shortcuts app to trigger `add-task.yml`
- **Morning digest persistence fix**: `_check_digest` now uses `>=` for hour check and persists `_last_digest_date` to config — daemon correctly fires the digest on first wake if laptop was off at 9am. (Implemented but daemon not yet restarted to pick up.)

---

## 16. Known issues / quirks

- **Widget fold button**: double-click the stats bar to fold. The old ▾ button in a title bar was removed when the title bar was removed. The fold interaction is not immediately discoverable.
- **Widget alpha on Wayland**: `self.attributes("-alpha", 0.78)` must be set in `self.after(80, ...)` (deferred 80ms) so the window is fully mapped before the compositor attribute is applied. Setting it in `_setup_window()` directly doesn't stick on GNOME/Wayland.
- **HiDPI blurriness**: `tk.call("tk", "scaling", 2.667)` makes fonts crisp but widget may appear larger than 250px on screen at 200% scale depending on how XWayland reports pixel counts to the compositor. This is a fundamental XWayland limitation — tkinter has no native Wayland support.
- **Scrollbar**: exists in the widget but is hidden (`vsb` is created but not packed). Mousewheel scrolling works.
- **`notified_due` reset**: once a task is notified, `notified_due=1` forever. If you reschedule a task's due date, you need to manually reset: `taskman` TUI → edit task, or direct SQL.

---

## 17. Development workflow

All code lives in the single file `taskman.py`. The scratchpad copy lives at:
```
/tmp/claude-0/.../scratchpad/taskman.py
```
After edits, the file is committed to the device:
```python
device_commit_files([{"devicePath": "~/.local/bin/taskman", "stagedPath": "...", "force": True}])
```
To pick up changes: `pkill -f "taskman widget"; sleep 0.5; taskman widget &`  
For daemon changes: `systemctl --user restart taskman-daemon`

---

## 18. Full dependency list

| Dependency | Install | Used for |
|-----------|---------|---------|
| Python 3.10+ | system | everything |
| `textual` | `pip install textual --break-system-packages` | TUI |
| `tkinter` | `sudo apt install python3-tk` | widget |
| `python-xlib` | `pip install python-xlib --break-system-packages` | rounded corners on X11 only (not needed on Wayland) |
| `notify-send` | system (libnotify-bin) | desktop notifications |
| `git` | system | sync |
| `sqlite3` | stdlib | database |
| `urllib` | stdlib | ntfy HTTP calls |
| `subprocess` | stdlib | notify-send, git |
| `threading` | stdlib | daemon loop |
