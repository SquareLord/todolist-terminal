# taskman

A terminal-native task manager built around the same mental model as the Notion Planner — subjects, priority scoring, and a work-by date — but living entirely in your shell, a floating desktop widget, or a full-screen TUI.

---

## Install

### 1. Place the Python source

```bash
mkdir -p ~/.local/lib
cp taskman.py ~/.local/lib/taskman.py
```

### 2. Create the `taskman` shim

```bash
cat > ~/.local/bin/taskman << 'EOF'
#!/bin/bash
exec python3 ~/.local/lib/taskman.py "$@"
EOF
chmod +x ~/.local/bin/taskman
```

> **Why a shim?** A plain executable Python file loses its `+x` bit when synced to the device. The shim lives at `~/.local/bin/taskman` and never changes — only the source at `~/.local/lib/taskman.py` is updated, and that file doesn't need to be executable.

### 3. Create the `task` short alias

```bash
cat > ~/.local/bin/task << 'EOF'
#!/bin/bash
exec taskman "$@"
EOF
chmod +x ~/.local/bin/task
```

### 4. Make sure `~/.local/bin` is on your `PATH`

```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
source ~/.bashrc
```

### 5. Dependencies

| Dependency | Required for | Install |
|---|---|---|
| Python 3.8+ | everything | — |
| `textual` | TUI (`taskman`) | `pip install textual --break-system-packages` |
| `python3-tk` | floating widget | `sudo apt install python3-tk` |
| `notify-send` | desktop notifications | `sudo apt install libnotify-bin` |
| `ntfy.sh` topic | phone notifications | see [Configuration](#configuration) |

---

## Data

Tasks are stored in a SQLite database at:

```
~/.local/share/taskman/tasks.db
```

Configuration is stored alongside it in `config.json`. Both are created automatically on first run.

---

## Quick start

```bash
task add "Study for CMSC351 exam" --kind deadline --importance high --date 10312026 --time 1000a --hours 6
task list
task status 1        # cycle: todo → wip → done
task list 10         # show 10 tasks instead of the default 5
```

---

## CLI reference

### `task add`

```
task add "Title" [options]
```

| Flag | Short | Description |
|---|---|---|
| `--subject NAME` | `-S` | Subject / category (Personal, Career, cmsc351, …) |
| `--importance LEVEL` | `-i` | `high`, `med`, or `low` (default: `med`) |
| `--date MMDDYYYY` | | Due date, e.g. `10312026` |
| `--time HHMMa\|HHMMp` | | Due time, e.g. `1159p` (use with `--date`) |
| `--due DATETIME` | `-d` | ISO due date, e.g. `"2026-10-31 23:59"` |
| `--hours N` | `-H` | Estimated hours |
| `--kind KIND` | `-k` | Task kind — see [Kinds](#kinds) (default: `deadline`) |
| `--notes TEXT` | `-n` | Notes |
| `--pin` | | Pin task to top of list |

**Interactive prompts by kind:**

- `--kind habit` → prompts *"How often? (daily/weekly/monthly)"* and stores the recurrence
- `--kind waiting` → prompts *"What are you waiting on?"* and stores it in notes

### `task list [N]`

Show the top N open tasks sorted by priority score (default: 5). Excludes `someday` and `waiting` tasks.

```bash
task list          # top 5
task list 10       # top 10
task list --all    # everything
task list -S cmsc351   # filter by subject
```

**Columns:** `ID · St · Imp · Title · Subj · Due/Wb`

The **Due/Wb** column shows the *work-by* date when estimated hours are set — the point at which you need to start to finish on time (due date minus 1.5× the estimated hours as a buffer). Otherwise it shows the due date directly. Overdue tasks are highlighted in red.

### `task backlog`

All open tasks grouped by subject, with total estimated hours per group. Good for a full weekly review.

### `task someday`

Show the Someday / Maybe list — tasks with `--kind someday`. These are aspirational ideas that don't belong in the active queue.

### `task done-today`

Tasks completed today.

### `task status <id> [STATUS]`

Cycle a task's status (`todo → wip → done → todo`) or jump directly:

```bash
task status 3         # cycle to next
task status 3 done    # set directly
```

Completing a `habit` task automatically spawns the next recurrence.

### `task edit <id>`

Interactive field-by-field wizard. Press Enter to keep the current value for any field.

```
Editing task #3: Morning workout

  Title [Morning workout]:
  Subject [Personal]:
  Importance (high/med/low) [med]:
  Due (MMDDYYYY or YYYY-MM-DD HH:MM) [none]:
  Est. hours [none]:
  Kind (deadline/habit/project/quick/waiting/someday) [habit]:
  Notes [none]:
```

If you change the kind to `habit`, it prompts for recurrence. If you change it to `waiting`, it prompts for what you're waiting on.

### `task ping <id>`

Send an immediate desktop notification (and phone ping if ntfy is configured) for the task.

### `task rm <id>`

Delete a task permanently.

### `task subjects`

List all subjects with open task counts, someday count, waiting count, and total estimated hours:

```
  Subject               Open   Smdy   Wait   ~Hrs
  ────────────────────────────────────────────────
  Career                   2      0      1    4.0h
  Personal                 5      3      0    8.5h
  cmsc351                  4      0      0   12.0h
```

### `task export [--output PATH]`

Export all active (non-done, non-someday/waiting) tasks to `tasks.json`. Used by the GitHub Actions sync workflow.

### `task sync [--repo PATH]`

Pull from GitHub, import any tasks added from a phone (`incoming/*.json`), then export and push `tasks.json`. Requires a sync repo configured via `task config`.

### `task config`

```bash
task config                          # show current config
task config --ntfy-topic YOUR_TOPIC  # set phone notification topic
task config --ntfy-server URL        # use a self-hosted ntfy server
task config --sync-repo /path/repo   # set git sync repo
task config --test                   # send a test notification to your phone
```

### `task daemon`

Run the notification daemon in the foreground. Watches for tasks due within 15 minutes and sends a morning digest at 9am.

### `task widget`

Launch a floating desktop widget (requires `python3-tk`). Always-on-top, draggable, scrollable. Click a task to cycle its status; right-click for the full menu.

### `taskman` (no arguments)

Opens the full-screen **TUI**.

---

## Kinds

The `kind` field replaces the old high/med/low-only model with a taxonomy that reflects how tasks actually differ:

| Kind | Description | Interactive prompt |
|---|---|---|
| `deadline` | Hard cutoff — an exam, a submission, a meeting. Default. | — |
| `habit` | Repeating task that should happen every day/week/month. | How often? |
| `project` | Ongoing work without a hard deadline (a portfolio, a side project). | — |
| `quick` | Under ~30 minutes — reply to an email, file a form. | — |
| `waiting` | Blocked on someone else. Hidden from the default list. | What are you waiting on? |
| `someday` | Aspirational — things you want to do eventually. Hidden from the default list. | — |

`waiting` and `someday` tasks are excluded from `task list` and `task backlog`. They live in their own views: `task someday` and the TUI's Waiting view.

---

## Priority scoring

Tasks in `task list` are ranked by a priority score:

```
score = importance_weight × urgency

importance_weight : HIGH=3  MED=2  LOW=1
urgency           : 1 / max(days_until_due, 0.25)
                    8.0  if overdue
                    0.5  if no due date
```

This means a high-importance task due in 2 days outranks a low-importance task due tomorrow, but an overdue medium-importance task jumps to the front of the queue.

---

## TUI

Launch with `taskman` (no arguments).

### Keybindings

| Key | Action |
|---|---|
| `a` | Add task |
| `e` | Edit task |
| `Space` | Cycle status (todo → wip → done) |
| `D` | Delete task |
| `P` | Toggle pin |
| `L` | Toggle someday |
| `p` | Ping (send notification) |
| `n` | New subject |
| `r` | Refresh |
| `q` | Quit |
| `1` | Open view |
| `2` | Backlog view |
| `3` | Today view |
| `4` | Overdue view |
| `5` | Done Today view |
| `6` | Someday view |
| `7` | Waiting view |

The sidebar shows views on top and subjects below. Clicking a subject filters the current view to that subject.

---

## Configuration

### Phone notifications via ntfy.sh

1. Install the [ntfy app](https://ntfy.sh) on your phone
2. Create a topic (e.g. `abhiram-taskman`)
3. Subscribe to it in the app
4. Configure taskman:
   ```bash
   task config --ntfy-topic abhiram-taskman
   task config --test   # verify it works
   ```

You'll get a morning digest of your top 5 tasks at 9am, plus overdue alerts whenever the daemon is running. The daemon runs automatically in the background when the TUI or widget is open, or you can run `task daemon` standalone.

### GitHub sync (cross-device)

This enables tasks added from a phone (via a shortcut that writes `incoming/*.json` to a git repo) to show up on your laptop automatically.

```bash
task config --sync-repo ~/path/to/your-sync-repo
task sync   # pull incoming, export tasks.json, push
```

---

## Database migration

If you're upgrading from an older version that used the `parking_lot` flag, `init_db` runs automatically on startup and migrates all `parking_lot=1` tasks to `kind='someday'`. No manual steps needed.

---

## File layout

```
~/.local/bin/taskman          ← shim (chmod +x, never changes)
~/.local/bin/task             ← short alias shim
~/.local/lib/taskman.py       ← Python source (update this one)
~/.local/share/taskman/
    tasks.db                  ← SQLite database (WAL mode)
    config.json               ← ntfy topic, sync repo path
```
