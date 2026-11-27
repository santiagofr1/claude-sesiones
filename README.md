# claude-sesiones

**A session manager for Claude Code: list, search, send to a trash folder, and clean up what's left over.**

Claude Code stores every session as a file under `~/.claude/projects`, and over time that grows to hundreds of
MB. There is no built-in way to delete a single session, or to get rid of the ones left behind by projects that
no longer exist. On top of that, the ruflo plugin leaves `.claude-flow` folders scattered across the disk.

`ses` shows you what's there, deletes safely (everything goes to a recoverable trash), and cleans up the junk
without hunting for files by hand.

## Features

- List and search sessions by text, project, or size.
- Show a session's details and the command to resume it.
- Move sessions to a recoverable trash.
- Restore anything from the trash.
- Clean up junk: empty or broken sessions, orphaned artifacts, and empty project folders.
- Clean up old data: sessions unused for N days, unused project memories, and `.claude-flow` folders.
- Empty the trash permanently.

## Installation

Requires Python 3.8 or later. No external dependencies.

### Debian / Ubuntu and derivatives: apt repository (recommended)

Add the repository once, and `ses` updates along with the rest of the system:

```sh
sudo install -d -m 0755 /etc/apt/keyrings
curl -fsSL https://santiagofr1.github.io/apt/key.asc | sudo gpg --dearmor -o /etc/apt/keyrings/santiagofr1.gpg
echo "deb [signed-by=/etc/apt/keyrings/santiagofr1.gpg] https://santiagofr1.github.io/apt stable main" | sudo tee /etc/apt/sources.list.d/santiagofr1.list
sudo apt update
sudo apt install claude-sesiones
```

### From source

```sh
git clone https://github.com/santiagofr1/claude-sesiones.git ~/Git/claude-sesiones
ln -s ~/Git/claude-sesiones/ses.py ~/.local/bin/ses
```

## Quick start

```sh
ses list all                   # every session, newest first
ses search                     # interactive search
ses show 3f2a                  # details of one session
ses delete junk --dry-run      # see what junk there is, without touching anything
ses delete old 30 --dry-run    # see what's older than 30 days
ses restore                    # list the trash
```

> The command output is in Spanish. The interactive search screen is in English.

## Commands

Wherever a command takes an `<id>`, you can pass the full UUID or a prefix of 4 or more characters. If a prefix
matches more than one session, nothing is done.

### `list`

Lists sessions, newest first, with a total at the end. A text argument matches the title, project, and id.

| Option | Description |
|---|---|
| `all` | All sessions (same as passing nothing). |
| `old <days>` | Only sessions unused for more than `<days>`. The number of days is required. |
| `--project SUBSTR` | Only sessions whose project contains `SUBSTR`. |
| `--orphans` | Only sessions whose project no longer exists. |
| `--sort date\|size` | Sort by date (default) or by size. |
| `--json` | JSON output instead of a table. |

```sh
ses list all                   # all sessions
ses list old 30                # unused for more than 30 days
ses list arduino --sort size   # match "arduino", largest first
ses list --orphans             # sessions from projects that no longer exist
ses list --project notes       # only sessions from projects containing "notes"
```

Example output (made-up data):

```
ID        MODIFICADA        TAMANO    MSJ  PROYECTO                 TITULO
3f2a9c10  2026-11-20 18:42  12.4 MB   236  ~/Git/sample-project     refactor del colector de datos
9c1d7e54  2026-11-18 09:15  340.2 KB   41  ~/Git/old-experiment     [borrado] probar el parser
b07e4a21  2026-11-21 21:03  1.8 MB     97  ~/Git/demo-app           [activa] pantalla de exportar
c5f08d3e  2026-10-02 11:27  88 B        2  ~/Downloads              prueba rapida

4 sesiones, 14.6 MB en total
```

The columns are id, last modified, size, message count, project, and title. Two marks can appear after the
title:

- `[borrado]`: the project folder no longer exists.
- `[activa]`: modified less than `SES_ACTIVA_MIN` minutes ago (the session may be in use).

### `search`

Searches inside the conversations: what you wrote and what Claude replied, plus the title and project. It does
not look at tool outputs.

How matching works:

- A session matches if it contains **any** of the words.
- Case and accents are ignored.
- Words shorter than 3 letters and very common words (`que`, `para`, `con`, `the`, `and`...) are ignored,
  since they match everything.
- Sessions with the most distinct matching words come first; ties go to sessions with the word in the title.

#### Interactive mode

With no text, `ses search` opens an interactive search: as you type, matching sessions appear instantly below,
each with a snippet of the message where the word is highlighted. Sessions are loaded once when it opens, so
filtering is immediate. Type words directly, without quotes.

```
search: ventilador
3 sessions   arrows: move   Enter: resume   Tab: details   Ctrl-U: clear   Esc: quit

03a37286  2026-11-25 14:31    6.0 MB  ¿se puede estabilizar la lectura de temperatura?
    ...da control fino del ventilador. Lo que se puede hacer: leer las RPM de los 2 ventiladores...
```

| Key | Action |
|---|---|
| Arrows, PgUp/PgDn | Move the selection |
| Enter | Resume the session (`cd` into the project and run `claude --resume`) |
| Tab | Show details (same as `ses show`) |
| Ctrl-U / Ctrl-W | Clear everything / delete the last word |
| Esc | Quit |

#### Non-interactive mode

With quoted text, it prints the results and exits, which is handy for scripts and with `--json`. For each
matching session it shows the full id, title, date, size, project, and how many times each word appears.

| Option | Description |
|---|---|
| `"text"` | One or more words, in quotes. |
| `--project SUBSTR` | Only sessions whose project contains `SUBSTR`. |
| `--json` | JSON output. |

```sh
ses search                     # interactive search
ses search "fan victus"        # sessions mentioning "fan" or "victus"
```

### `show`

Shows a session's details: project, dates, size including artifacts, message count, the first and last user
messages, and the `claude --resume` command to pick it back up.

```sh
ses show 3f2a                  # details of the session starting with 3f2a
```

### `delete`

Moves sessions to the trash. If it was the last session of its project, the project's memory goes with it.

| Option | Description |
|---|---|
| `<id> [<id>...]` | One or more sessions by id or prefix. |
| `old <days>` | Sessions unused for more than `<days>`, unused project memories, and **all** `.claude-flow` folders under your home, regardless of age. Memories of projects with no sessions go to `_huerfanos`. |
| `junk` | Everything broken, regardless of age: 0-byte sessions, files with no valid JSON line, sessions with no messages, orphaned artifacts (a session folder without its `.jsonl`), and empty project folders. Does not touch `.claude-flow` folders. |
| `--dry-run` | With `old` or `junk`: show what would be moved without touching anything. |
| `--project SUBSTR` | With `old` only: filter sessions and memories by project. Does not filter `.claude-flow` folders. |

```sh
ses delete 3f2a 9c1d               # send both to the trash
ses delete old 30 --dry-run        # what it would take, without touching anything
ses delete old 90 --project rtop   # old sessions from projects containing "rtop"
ses delete junk --dry-run          # see what junk there is, without touching anything
ses delete junk                    # send the junk to the trash
```

### `restore`

With no arguments, lists what's in the trash. With an `<id>`, puts the session back where it was, along with its
memory if it took it. It never overwrites: if the destination already exists, nothing is touched.
`.claude-flow` folders show up as `[flow]`.

```sh
ses restore                    # list the trash
ses restore 3f2a               # put the session (and its memory) back
```

### `empty`

Permanently deletes the trash, without asking.

| Option | Description |
|---|---|
| `--dry-run` | Show how much would be deleted without touching anything. |

```sh
ses empty --dry-run            # how much the trash takes up, without deleting
ses empty                      # delete the trash for good
```

## Safety

- The trash lives in `~/.claude/ses-papelera`. Nothing is deleted permanently except by `ses empty`.
- Sessions modified in the last 10 minutes are never touched, in case Claude Code is using them.
- `delete old`, `delete junk`, and `empty` accept `--dry-run` so you can check first.
- A project's memory leaves with its last session, and comes back with `restore`.
- The home project's memory is protected and never touched.
- Paths are checked before anything is moved or deleted: `ses` only operates inside `projects`, `file-history`,
  `session-env`, and the trash. It does not follow symlinks.

## Configuration

Optional environment variables:

| Variable | Default | Description |
|---|---|---|
| `CLAUDE_HOME` | `~/.claude` | Claude Code root folder. The trash lives in `$CLAUDE_HOME/ses-papelera`. |
| `SES_ACTIVA_MIN` | `10` | Minutes since the last modification for a session to count as active. Active sessions are never touched. |
| `SES_PROTEGER_HOME` | `1` | Set to `0` to stop protecting the home project's memory. |
| `SES_FLOW_ROOT` | `$HOME` | Root where `delete old` looks for `.claude-flow` folders. |
| `NO_COLOR` | (unset) | Any non-empty value disables colored output. |

## Tests

```sh
python3 -m unittest discover -s tests
```

The tests use temporary folders with their own `CLAUDE_HOME`. They never touch your real `~/.claude`.
