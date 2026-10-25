# Console Agents (`ca`) Command Reference & Combinations Guide

This document provides a comprehensive reference of all CLI commands, shell commands, options, and practical command combinations for Console Agents (`ca`).

---

## 1. Global Options & Flags

Global flags apply to the root `ca` command and can be combined with any subcommand:

| Global Flag | Description | Example |
|---|---|---|
| `-C <dir>`, `--dir <dir>` | Specify target project directory | `ca -C W:\code\shop status` |
| `-P <name\|n>`, `--project <name\|n>` | Select registered project by name or index | `ca -P shop chats` or `ca -P 2 chats` |
| `--chat <n\|id>` | Select target chat number or ID | `ca --chat 3 log` |
| `-q`, `--quiet` | Suppress live agent streaming activity | `ca -q run "Run tests"` |
| `--version` | Display version information | `ca --version` |

---

## 2. Common Execution Flags

These options apply to commands that execute turns (`run`, `continue`, `/new`, `/continue`):

| Flag | Description | Values / Examples |
|---|---|---|
| `--agent <name>` | Force or prioritize a specific agent | `--agent claude`, `--agent codex`, `--agent antigravity`, `--agent agy-claude`, `--agent agy-gpt` |
| `--max-turns <N>` | Cap the maximum number of turns for this run | `--max-turns 5`, `--max-turns 20` |
| `--permission <mode>` | Override tool permission level | `--permission full` (default autonomous), `--permission edits` (edits only), `--permission read` (read-only) |
| `--no-wait` | Exit immediately instead of waiting when all agents hit quota limits | `--no-wait` |

---

## 3. Command Syntax & All Combination Types

### 3.1 Starting Tasks: `ca run` (aliases: `r`, `task`, `t`)

#### Syntax:
```bash
ca run <task...> [options]
ca r <task...> [options]
```

#### Combinations:
* **Basic task in foreground:**
  ```bash
  ca run "Fix authentication timeout in auth.py"
  ca r "Implement user registration"
  ```
* **Run in background (`--bg`):**
  ```bash
  ca run "Refactor database models" --bg
  ca r "Run test suite and fix failing tests" --bg
  ```
* **Force planning / roadmap (`--plan`):**
  ```bash
  ca run "Build new billing module" --plan
  ca r "Migrate to PostgreSQL" --plan --bg
  ```
* **Bypass planning for quick tasks (`--no-plan`):**
  ```bash
  ca run "Fix typo in README" --no-plan
  ca r "Update package version" --no-plan --agent codex
  ```
* **Specific agent with background & turn limits:**
  ```bash
  ca run "Debug memory leak" --agent claude --bg --max-turns 10
  ca r "Write unit tests for parser" --agent codex --no-wait
  ```
* **Permission restrictions:**
  ```bash
  ca run "Review code security" --permission read
  ca r "Apply formatting only" --permission edits
  ```
* **Cross-project task execution (`-P` or `-C`):**
  ```bash
  ca -P shop run "Add checkout coupon feature" --bg
  ca -C W:\code\mobile r "Fix adb reconnect issue" --agent codex
  ```
* **Read task from standard input (`-`):**
  ```bash
  cat task.txt | ca run -
  git diff | ca r "Review this diff for bugs" -
  ```

---

### 3.2 Continuing Tasks: `ca continue` (aliases: `c`)

#### Syntax:
```bash
ca continue [options]
ca c [options]
```

#### Combinations:
* **Continue current chat after limit reset or pause:**
  ```bash
  ca continue
  aa c
  ```
* **Send message or answer to agents (`-m`):**
  ```bash
  ca continue -m "Use port 8080 instead of 3000"
  ca c -m "Yes, proceed with migration"
  ```
* **Continue specific chat by number or project prefix:**
  ```bash
  ca --chat 4 continue -m "Approved"
  ca -P shop --chat 2 c -m "Run tests now"
  ```
* **Continue in background (`--bg`):**
  ```bash
  ca continue -m "Add documentation" --bg
  ca c --bg
  ```
* **Fresh session restart (discard agent chat cache to save tokens or fix stalls):**
  ```bash
  ca continue --fresh
  ca c -m "Try a different approach" --fresh --agent claude
  ```
* **Agent override and turn bounds on continue:**
  ```bash
  ca c --agent codex --max-turns 5 --no-wait
  ```

---

### 3.3 Listing Chats: `ca chats` (aliases: `ls`)

#### Syntax:
```bash
ca chats [options]
ca ls [options]
```

#### Combinations:
* **List chats of current project:**
  ```bash
  ca chats
  ca ls
  ```
* **List chats across all projects (`--all`):**
  ```bash
  ca chats --all
  ca ls --all
  ```
* **Control chat history limit (`-n` or `--all-chats`):**
  ```bash
  ca ls -n 5
  ca ls -n 50
  ca ls --all-chats
  ca ls --all --all-chats
  ```

---

### 3.4 Opening and Monitoring Chats: `ca open` (aliases: `o`)

#### Syntax:
```bash
ca open [chat_ref]
ca o [chat_ref]
```

#### Combinations:
* **Open active/current chat:**
  ```bash
  ca open
  aa o
  ```
* **Open chat by number:**
  ```bash
  ca open 2
  ca o 5
  ```
* **Open chat in specific project:**
  ```bash
  ca open shop#3
  ca -P shop o 2
  ```

---

### 3.5 Stopping Chats: `ca stop` (aliases: `x`)

#### Syntax:
```bash
ca stop [chat_ref]
ca x [chat_ref]
```

#### Combinations:
* **Stop current background chat:**
  ```bash
  ca stop
  aa x
  ```
* **Stop specific chat:**
  ```bash
  ca stop 3
  ca x shop#2
  ca -P shop x 1
  ```

---

### 3.6 Deleting Chats: `ca delete` (aliases: `rm`, `del`)

#### Syntax:
```bash
ca delete [chat_refs...] [options]
ca rm [chat_refs...] [options]
```

#### Combinations:
* **Delete specific chat (with confirmation):**
  ```bash
  ca delete 3
  ca rm 2
  ```
* **Delete multiple chats at once:**
  ```bash
  ca rm 1 2 4
  ```
* **Delete all finished chats (`--done`):**
  ```bash
  ca rm --done
  ca rm --done -y
  ```
* **Force delete running chat and skip confirmation (`-f`, `-y`):**
  ```bash
  ca rm -y 3
  ca rm -f -y 2
  ca rm -f -y --done
  ```
* **Delete chat in another project:**
  ```bash
  ca -P shop rm 1
  ca rm shop#1
  ```

---

### 3.7 Process Inspection & Cleanup: `ca ps` & `ca kill` (alias: `k`)

#### Combinations:
* **List background processes started by agents in this project:**
  ```bash
  ca ps
  ```
* **List background processes across all projects:**
  ```bash
  ca ps --all
  ```
* **Kill specific process by PID:**
  ```bash
  ca kill 14280
  ca k 14280 8920
  ```
* **Kill all processes started by agents for this project:**
  ```bash
  ca kill all
  ca k all
  ```
* **Kill all processes across every project:**
  ```bash
  ca kill all --all
  ca k all --all
  ```

---

### 3.8 Chat Inspection: `ca status`, `ca roadmap`, `ca log`

#### Combinations:
* **View status of current or specific chat:**
  ```bash
  ca status
  ca s 3
  ca s shop#2
  ```
* **View roadmap:**
  ```bash
  ca roadmap
  ca rd 4
  ca rd shop#1
  ```
* **View steps log:**
  ```bash
  ca log
  ca l 2
  ca l -n 10
  ca l 3 -n 50
  ```
* **View full conversation transcript:**
  ```bash
  ca log --transcript
  ca l 2 --transcript
  ca l --transcript --chars 15000
  ```

---

### 3.9 Usage & Account Quota Checks: `ca usage` (aliases: `u`)

#### Combinations:
* **Display usage summary and reset times:**
  ```bash
  ca usage
  aa u
  ```
* **Live quota refresh without model prompts (`refresh`):**
  ```bash
  ca usage refresh
  ca u refresh
  ca u refresh claude
  ca u refresh codex
  ca u refresh antigravity
  ```
* **Manual quota adjustment (`set`):**
  ```bash
  ca usage set codex 70
  ca u set codex 70 --window weekly
  ca u set claude 50 --window five_hour --resets 2h30m
  ca u set antigravity 80 --resets 18:00
  ```
* **Clear cooldown / rate limit pause (`clear`):**
  ```bash
  ca usage clear antigravity
  ca u clear claude
  ca u clear codex
  ```
* **Reset all recorded usage numbers and refresh cache (`reset`):**
  ```bash
  ca usage reset
  ca u reset
  ```

---

### 3.10 Agent Discovery: `ca agents` (aliases: `a`)

* **List detected agent CLIs and status:**
  ```bash
  ca agents
  ca a
  ```

---

### 3.11 Project Management: `ca project` (aliases: `p`, `projects`)

#### Combinations:
* **List registered projects:**
  ```bash
  ca project
  aa p
  ca p list
  ```
* **Add a project directory with optional description:**
  ```bash
  ca p add shop W:\code\shop -d "E-commerce platform"
  ca p add api W:\code\api
  ca p add . -d "Current working directory"
  ```
* **Add a project by cloning a git repository** (https://, ssh://, git://, file://, `git@host:owner/repo` or a bare `*.git` path):
  ```bash
  ca p add https://github.com/me/shop.git                  # clones into ./shop, project "shop"
  ca p add https://github.com/me/shop.git W:\code          # W:\code is not empty: clones into W:\code\shop
  ca p add web git@github.com:me/shop.git W:\code\web      # own name; a new or empty folder is used as is
  ```
  A folder that already is a clone of the same repository is registered without cloning again.
* **Switch default project:**
  ```bash
  ca p use shop
  ca p use 2
  ```
* **Rename a project:**
  ```bash
  ca p rename shop store
  ca p mv store webshop
  ```
* **Remove a project from registry (files remain untouched):**
  ```bash
  ca p remove webshop
  ca p rm 3
  ```
* **Show project information:**
  ```bash
  ca p info
  ca p info shop
  ```

---

### 3.12 Memory Management: `ca memory` (aliases: `m`, `mem`)

#### Combinations:
* **List memory entries:**
  ```bash
  ca memory
  aa m
  ca m -p          # only current project memory
  ca m -g          # only global memory
  ```
* **Add memory entry:**
  ```bash
  ca m add "Always run tests with python -m unittest discover -s tests"
  ca m add -g "Host OS is Windows 11 with local admin privileges"
  ```
* **Remove memory entry by ID:**
  ```bash
  ca m rm p1
  ca m rm -g g2
  ```
* **Clear memory entries:**
  ```bash
  ca m clear -y
  ca m clear -g -y
  ```

---

### 3.13 Settings & Configuration: `ca settings` (aliases: `st`), `ca config` (aliases: `cf`)

#### Combinations:
* **Display settings:**
  ```bash
  ca settings
  aa st
  ca st --all      # include all per-agent settings
  ```
* **Read a specific setting:**
  ```bash
  ca st get run.max_turns
  ca st get balancer.soft_limit
  ```
* **Set project-level settings:**
  ```bash
  ca st set run.max_turns 30
  ca st set run.permission edits
  ca st set balancer.soft_limit 0.75
  ca st set agents.codex.enabled true
  ca st set run.push on_done
  ca st set run.leftover_processes kill
  ```
* **Set global settings (`-g` / `--global`):**
  ```bash
  ca st set -g run.permission full
  ca st set -g balancer.hard_limit 0.95
  ca st set -g agents.antigravity.enabled true
  ```
* **Unset / revert setting to default:**
  ```bash
  ca st unset run.max_turns
  ca st unset -g run.permission
  ```
* **View or create configuration files (`config` / `cf`):**
  ```bash
  ca config --show
  ca cf --show
  ca cf            # creates project .autoagents/config.json
  ca cf --global   # creates global ~/.autoagents/config.json
  ```

---

### 3.14 Direct System & Automation Commands

* **Git push without invoking agents:**
  ```bash
  ca push
  ```
* **Enable Shut Down When Finished (locks phone screen & powers down PC when all chats complete):**
  ```bash
  ca swf
  ```
* **Cancel Shut Down When Finished:**
  ```bash
  ca qqq
  ```

---

### 3.15 App Updates and Rollback

Updates affect the ConsoleAgents installation repository, independent of `-C`, `-P` or the
open chat. Requires Git, a Git clone, a configured remote (normally GitHub `origin`) and
network access for updates. Stop active chats and commit or stash installation edits first.

```bash
ca update --check             # fetch and show incoming commits without applying them
ca update                     # display changes and ask before applying
ca update -y                  # apply without confirmation
ca update --target v1.2.0      # check out a specific available ref/tag/commit
ca rollback --list            # list checkpoints, newest first
ca rollback                   # interactive table, selection, then confirmation
ca revert 1                   # restore checkpoint 1, with confirmation
ca rollback <commit> -y        # restore an explicit local commit without prompts
ca update --rollback          # open the same rollback interface
```

`update` aliases: `up`, `upgrade`. `rollback` aliases: `revert`, `rb`.
The shell accepts the same options with a slash: `/update --check`, `/update -y`,
`/rollback --list`, `/rollback`, `/revert 1`.

| Command | Options | Meaning |
|---|---|---|
| `update` | `--check`, `-c` | Fetch and inspect updates only |
| `update` | `--target <ref>` | Check out an available ref instead of pulling; a tag or commit can leave HEAD detached, requiring a branch checkout before later updates |
| `update` | `--rollback`, `-r` | Use the rollback interface; `--target <ref>` selects its target |
| `rollback` | `[checkpoint_ref]` or `--target <ref>` | Checkpoint number (1 = newest) or local Git ref/commit |
| `rollback` | `--list`, `-l` | Show checkpoint dates, commits, actions and summaries |
| Both | `--yes`, `-y` | Skip confirmation; rollback without a target chooses the newest checkpoint |
| Both | `--allow-dirty` | Bypass the tracked local-edit guard; rollback can discard uncommitted edits |

Normal updates use `git pull --ff-only`, refusing divergent history. Each update and rollback
saves the previous committed version in `.autoagents/update_history.json` (up to 50 entries).
The interactive rollback table accepts a checkpoint number, or `q`/empty input to cancel,
then requires `y` to restore it. Rollback needs no network: it resets the current branch and
tracked files to a locally available commit. Checkpoints do not back up uncommitted edits.
Restart `ca` after changing versions to load the new code.

If the shell fails to open, run `ca rollback` directly in a terminal, or
`python /path/to/ConsoleAgents/ca.py rollback` when the launcher is unavailable.
This fallback still requires a working Python interpreter and CLI package.

---

## 4. Interactive Shell Slash Commands

When running inside the interactive `ca` console shell, the following commands are available:

| Shell Command | Equivalent CLI Command | Description |
|---|---|---|
| `<task text>` | `ca run "<task>" --bg` | Start new background chat and open it |
| `<message>` | `ca continue -m "<msg>"` | Send message/reply to the currently open chat |
| `/new <task>` (`/n`, `/task`, `/t`) | `ca run "<task>" --bg` | Start new chat (supports `--agent`, `--no-plan`, etc.) |
| `/plan <task>` | `ca run "<task>" --plan --bg` | Force creation of a roadmap |
| `/noplan <task>` | `ca run "<task>" --no-plan --bg` | Run directly without a roadmap |
| `/continue [msg]` (`/c`) | `ca continue [-m "<msg>"]` | Continue current chat |
| `/open <n\|proj#n>` (`/o`) | `ca open <n>` | Open and follow chat live |
| `/back` (`/b`, `/home`) | - | Return home from open chat (chat keeps working) |
| `/chats [all] [--details]` (`/ls`) | `ca chats [--all] [--details]` | List chats; include hidden fields on narrow screens |
| `/stop [n]` (`/x`) | `ca stop [n]` | Stop background chat |
| `/rm <n...> [--done] [-y] [-f]` | `ca delete ...` | Delete chats |
| `/ps [--all] [--details]` | `ca ps [--all] [--details]` | List tracked processes; include hidden fields on narrow screens |
| `/kill <pid...> \| all` (`/k`) | `ca kill ...` | Kill leftover processes |
| `/status` (`/s`) | `ca status` | View chat status |
| `/roadmap` (`/rd`) | `ca roadmap` | View roadmap |
| `/log` (`/l`) | `ca log` | View steps log |
| `/usage` (`/u`) | `ca usage` | View usage quotas |
| `/usage refresh [agent]` (`/u refresh`) | `ca usage refresh [agent]` | Check account quotas without prompt costs |
| `/agents` (`/a`) | `ca agents` | View agent availability |
| `/project [use <n\|name>]` (`/p`) | `ca project ...` | Switch or list projects |
| `/memory [add ... \| rm ...]` (`/m`) | `ca memory ...` | Manage memory entries |
| `/update [--check] [-y]` | `ca update ...` | Check or update the app from GitHub |
| `/rollback [ref] [--list]` (`/revert`, `/rb`) | `ca rollback ...` | Choose and restore a saved app version |
| `/settings [set ...]` (`/st`) | `ca settings ...` | Adjust settings |
| `/swf` / `/qqq` | `ca swf` / `ca qqq` | Toggle shut-down-when-finished |
| `/cd <dir>` | - | Change working directory inside shell |
| `/help` (`/?`) | - | Show command help |
| `/quit` (`/q`) | - | Exit the interactive shell |

### Narrow screens and pinned status

The shell and CLI adapt to terminal width. Narrow usage lists show percentages instead of bars;
chat lists omit update timestamps, and process lists omit agent, age and memory fields.
`ca chats --details` / `/ls --details` and `ca ps --details` / `/ps --details` retain those fields
in wrapped blocks. `ca usage show codex` / `/u show codex` includes reset times, report age,
status and pacing. Desktop table columns remain available as before.

Agent display labels are orange **Claude**, blue **Gemini** (`antigravity`) and gray **GPT** (`codex`).
Use the original identifiers in command arguments and settings. Progress lines use only a
vendor-colored elapsed time, for example `0:03`, followed by the message.

The interactive shell and `ca open` pin local usage and tracked process count at the top of
compatible ANSI terminals. The header refreshes from local files every three seconds without
making account requests, and follows terminal resize and project switches. It may use up to
three rows; very narrow/short screens may not fit every enabled agent. Use `/u` for the full list.

| Header field | Meaning |
|---|---|
| `P:3` | Three unique live tracked processes across registered projects plus the current project, including agent CLIs and helpers; `P:?` means the count could not be read |
| `42%` | Highest usage across the agent's two quota windows |
| `42%~` | Local token estimate |
| `42%*` | Stale or cached account report |
| `?` | No usable usage data |
| `42%?` | Known usage in one window; another window is unknown |
| `-` | Both windows reported disabled |

When pinned status is active, the shell editor redraws typed input after asynchronous output.
Use arrows, Home/End, Delete and Backspace to edit; Up/Down recall this session's history,
Ctrl+U clears the line, and Ctrl+D on an empty line exits. Long input scrolls horizontally.
Bracketed multiline paste is submitted as one message, with newlines converted to spaces.
Ctrl+C leaves an open chat or exits from home. The scroll region, paste mode and POSIX input
settings are restored when the shell closes or raises an exception.

Set `CONSOLEAGENTS_HEADER=0` before launching to keep ordinary console input/output.
The header is also skipped when input/output is redirected, `TERM` is `dumb`/`unknown`, terminal
capabilities are unavailable, or the startup size is under 16 columns/four rows. Colors honor
`NO_COLOR`; pinning does not require colored text. Native Windows console and WSL POSIX PTY
checks have passed; actual Android/Termux execution remains unverified.
