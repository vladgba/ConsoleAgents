# Console Agents (`ca`)

A console relay that puts three agents to work on your project: **Claude Code** (`claude`),
**Codex CLI** (`codex`) and **Antigravity CLI** (`agy`, Google's Gemini models on your Google account).
Before each turn it picks an agent based on its usage limits. When one agent runs out mid-task, the
next agent gets the whole conversation and the saved roadmap and carries on.

Pure Python 3.10+, standard library only. Everything runs in the console.

## Quick start

```powershell
# from your project folder (or pass -C <dir>)
W:\Desktop\ConsoleAgents\ca.cmd run "Add JWT login to the API with tests"
```

Add `W:\Desktop\ConsoleAgents` to your PATH to run `ca` from anywhere.
On macOS/Linux, use the `ca` shell script instead, or run `python ca.py`.

| Command (short) | What it does |
|---|---|
| `ca` | the shell: chats run in the background, you open and leave them (see **Chats** below). It opens with each agent's usage, a short command list and the project's chats |
| `ca run "task" [--bg]` (`r`) | start a new chat. A complex task gets a roadmap first (`--plan` / `--no-plan` to force). `--bg`: in the background |
| `ca continue [-m "msg"] [--bg] [--fresh]` (`c`) | continue a chat after a limit, Ctrl+C or a question, or give a follow-up. If it is working, the message is queued for its next turn |
| `ca chats [--all]` (`ls`) | chats of this project (or every project): status, agent, task |
| `ca open [n]` (`o`) | watch a chat live and type into it; Ctrl+C leaves it working |
| `ca stop [n]` (`x`) | stop a chat that works in the background |
| `ca ps [--all]` | processes agents started that are still running (Gradle daemons, adb, dev servers…) |
| `ca kill <pid…> \| all` (`k`) | kill them, with their children. Only processes ca saw an agent start |
| `ca delete <n...>` (`rm`) | delete chats: `ca rm 3`, `ca rm 2 5`, `ca rm --done` (asks first, `-y` skips; `-f` also deletes a working chat). Memory and settings stay |
| `ca status [n]` / `ca roadmap [n]` / `ca log [n] [--transcript]` (`s` / `rd` / `l`) | look at a chat |
| `ca usage` (`u`) | 5-hour and weekly usage of each agent, reset times and status |
| `ca usage refresh [agent]` (`u refresh`) | force account quota checks for enabled agents, or one named agent; shell: `/usage refresh [agent]` |
| `ca u set codex 70 [--window weekly] [--resets 2h]` | tell the balancer how full an agent is |
| `ca u clear antigravity` | clear a cooldown (e.g. after you logged in again) |
| `ca update [--check] [-y]` | check or update this app from its GitHub remote; shell: `/update` |
| `ca rollback [ref] [--list]` (`revert`) | choose a saved version to restore; shell: `/rollback` or `/revert` |
| `ca agents` (`a`) | which CLIs were found |
| `ca project [list\|add\|use\|rename\|remove\|info]` (`p`) | your projects (see below) |
| `ca memory [add "text" [-g] \| rm p3 \| clear]` (`m`) | what the agents remember, per project and global |
| `ca settings [set <key> <value> [-g] \| unset <key> \| get <key>]` (`st`) | settings for this project or for all |
| `ca config [--global] [--show]` (`cf`) | create / show the config file |

> **Full Command Reference & Combinations Guide:** See [COMMANDS.md](COMMANDS.md) for an exhaustive guide detailing all subcommands, flags, options, and combination patterns.

Common flags: `--agent claude` (prefer an agent), `--max-turns N`, `--no-wait`, `--permission full|edits|read`, `-q`.
Pick the project with `-P <name>` (any folder) or `-C <dir>`; otherwise the project that contains the current folder is used.
A chat is `n` (its number in `ca ls`), `project#n` (e.g. `shop#3`) or `--chat <n>`; the default is the project's current chat.

### Common Command Combinations

```powershell
# Starting tasks with planning & agent overrides
ca run "Build auth system" --plan --bg                   # force roadmap and run in background
aa r "Fix quick typo" --no-plan --agent codex            # skip roadmap and force Codex
ca run "Refactor database models" --permission edits     # restrict agent to file edits only
ca run "Long analysis" --max-turns 10 --no-wait          # turn bound without waiting on limits
aa -P shop run "Add stripe billing" --bg                 # target specific project by name

# Continuing and messaging chats
ca continue -m "Approved, proceed with tests"            # send reply to current chat
aa c -m "Try a different approach" --fresh --bg          # start fresh session in background
aa --chat 3 c -m "Use port 8080"                         # continue specific chat by index

# Monitoring and process cleanup
ca chats --all -n 20                                     # list last 20 chats across all projects
ca open 2                                                # attach live to chat #2
ca ps --all                                              # inspect leftover background processes
ca kill all                                              # terminate all agent-spawned helpers

# Quota refresh and settings
ca usage refresh                                         # refresh live quota status without prompt costs
ca u set codex 70 --window weekly --resets 2h30m         # manually update quota utilization
ca settings set run.max_turns 25                         # set project-level turn limit
ca settings set -g run.permission full                   # set global autonomous execution
```

## How agents work (rules)

Every agent gets these rules, in this order:

1. **Plan if needed**: if the work is bigger than one focused change and the roadmap does not cover it,
   the agent proposes steps (`add_steps`, each rated light/mid/heavy) and **ends its turn without starting
   them**. Every step then goes to the agent best suited for its level, so a weak agent that found a hard
   task does not do the hard part itself.
2. **Delegate if needed**: it hands a self-contained part to another agent (`delegate`: title,
   self-contained details, level, agent): light parts for the helper agents, a part another agent is
   better at, or when its own usage is high. Delegated steps go into the roadmap right after the current
   step, and the named agent gets a strong bonus for them (`balancer.delegate_bonus`, 1.6). Usage limits
   still apply, so a full agent is never forced. A task is never finished while delegated work is still open.
3. **Do the work**: small, safe increments, the project's conventions, verified with build/tests.
4. **Commit every finished feature separately**: one commit per feature or step, staging only that
   feature's files, never pushing or rewriting history. Commits are listed in the report and logged
   (`📦 commit ...`). In a folder that is not a git repository, agents skip this (and do not create one).
   `ca settings set run.commit off` stops agents from committing.
5. **Pushing is done by aa, not by an agent** (it needs no AI, so it costs no tokens). Off by default; per
   project: `ca st set run.push on_done` pushes the checked-out branch whenever a chat finishes
   (`run.push_branches '["dev"]'` limits it to some branches). It never forces and never waits for a
   password: a failure is just reported. `ca push` (`/push`) pushes right now. Agents are told not to push.

A bug an agent notices outside its own step is **delegated inside the current chat** (rule 2), to the agent
that is better at it: e.g. Codex sees a tricky bug and delegates it to Claude; it becomes the next roadmap step.

**Weak agent, hard work.** Three more guards keep hard work with strong agents:
* Each agent is told its own profile (strength, the levels it takes). After finishing a step it may go on
  with the next one only if that step is rated the same level or lower.
* An agent that finds its step (or the task) harder than rated, or harder than it can do well, reports
  `too_hard`. The step is then rated one level higher, that agent is excluded from it, and a stronger agent
  takes over with a handover note. If every capable agent said "too hard", the best one tries again.
  `--agent` does not override this.
* Helper agents never get heavy or unrated work in the first place (see the levels above).

Change the rules with `ca settings set run.rules '["...", "..."]'` for one project, or add `--global`.

## Chats (like agent tabs)

Every task is a **chat**. Chats work in the **background**: a project can have several, and you can be in
one, leave it and come back later, like the agent tabs of AgentsRoom.

```
ca demo> add a --version flag to hello.py          <- a new chat starts in the background and opens
══ chat #4 · demo · working ══  add a --version flag to hello.py
  [codex 00:03] 💬 I'll inspect hello.py ...
ca demo #4> also mention it in the docstring        <- goes to this chat, read at its next turn
ca demo #4> /back                                    <- leave it, it keeps working
ca demo> /ls                                         <- every chat and what it is doing
✔ chat #4 (add a --version flag) is done  -> /open #4     <- you are told when one finishes or needs you
ca demo> /open 4                                     <- back in, with its recent output
```

* **At home**, typing a task starts a new chat. **In a chat**, whatever you type goes to that chat: an
  answer to its question, a correction while it works (read at the next turn, and the chat does not stop
  while a message is waiting), or a follow-up once it is done.
* `/back` or Ctrl+C leaves a chat; `/stop` stops it; `/rm 3` (or `/rm` inside a chat) deletes one; `/quit` closes the shell, and chats that are working
  **keep working**. Run `ca` again (or `ca ls --all`) to come back.
* Statuses: `working`, `waiting` (for usage limits), `needs input`, `done`, `stopped`.
* From the command line: `ca r --bg "task"`, `ca ls`, `ca o 4`, `ca c -m "answer" --chat 4`, `ca x 4`.
* Each background chat is a separate process. Its console output goes to `chats/<id>/live.log`, and the
  messages you type go to its inbox. Two chats in the same folder may edit the same files, and ca warns
  you when you start one while another is working.

Shell commands: `/new <task>` (`/n`, takes `--agent`, `--no-plan`...), `/open <n|proj#n>` (`/o`), `/back` (`/b`),
`/chats [all]` (`/ls`), `/continue [msg]` (`/c`), `/stop [n]` (`/x`), `/s /rd /l /u /a /p /m /st`, `/cd <dir>`,
`/update [--check]`, `/rollback [ref]` (`/revert`), `/help`, `/quit`. Any `ca ...` command line works in the shell too.

## Small screens and the pinned status header

The layout adapts to terminal width. On screens narrower than 81 columns, usage becomes
percentages, chat/process lists hide secondary columns, and long rows wrap into readable blocks.
Use `/ls --details`, `/ps --details` or `/u show codex` to see the additional fields.
The display names are **Claude** (orange), **Gemini** (blue, `antigravity`) and **GPT** (gray, `codex`).
Command arguments still use `claude`, `antigravity` and `codex`.

Interactive ANSI terminals get a pinned header, including when opening a chat with `ca open`:

```text
P:3 Claude 42% · GPT 8%~ · Gemini ?
```

`P:3` counts unique live processes tracked across your registered projects and the current project,
including agent CLIs and their helpers. Each agent shows the higher usage of its two quota windows.
`~` means a local estimate, `*` means stale/cached account data, and `?` means missing data.
A percentage followed by `?` means the other window is unknown; `-` means both windows are disabled.
Unknown usage is never presented as 0%. The header reloads local data every three seconds; use
`/u refresh` for a new account check. It uses up to three rows to fit narrow screens; `/u` shows all agents.

While the header is enabled, typing survives background output. Incoming output fragments are
joined into complete lines before redrawing your draft, preserving its cursor position.
Submitting a long draft shows the full message in the console history. Arrow keys, Home/End, Delete,
Backspace and session-only history work; long input scrolls horizontally. Bracketed multiline paste
becomes a single message. Ctrl+C leaves a chat or exits from home, and terminal settings are restored.
Turn progress prefixes are only a vendor-colored elapsed time such as `0:03`, without names or brackets.

Redirected output and unsupported terminals keep ordinary input/output. Very small terminals
(under 16 columns or four rows at startup) skip the header. To disable it explicitly, set
`CONSOLEAGENTS_HEADER=0` before launching (`$env:CONSOLEAGENTS_HEADER='0'` in PowerShell,
or `export CONSOLEAGENTS_HEADER=0` in Termux). `NO_COLOR` disables colors.

The header/editor has been checked in a native Windows console and POSIX pseudoterminals in WSL,
including background output, resize and Ctrl+C restoration. Actual Android/Termux execution has
not yet been verified.

## Update the app and recover a previous version

From the shell, run `/update --check` to fetch and display incoming commits, then `/update`
to confirm and apply a fast-forward update from the installation's GitHub remote.
These commands update the ConsoleAgents installation, regardless of your selected project.
Git and a Git clone with a configured remote are required; commit or stash local edits first.

```text
/update --check
/update
/rollback --list
/rollback
/revert 1
```

`/rollback` opens a small checkpoint table with dates, commits and summaries. Choose a number
(1 is newest) and confirm with `y`, or enter `q` to cancel. `/revert` is an alias.
Each update and rollback records the previous commit in `.autoagents/update_history.json`.
Checkpoints preserve committed versions, not uncommitted edits. Rollback resets the installation's
current branch and tracked files to the selected commit. `--allow-dirty` bypasses the local-edit
guard and can discard edits during rollback.

After changing versions, exit and restart `ca` to load the changed code. Stop active chats before
changing the installation so background agents do not run across versions.
If the shell cannot open after an update, use the standalone fallback from a terminal:

```bash
ca rollback --list
ca rollback                 # select a checkpoint and confirm
ca rollback 1 -y            # restore newest checkpoint without prompts
python /path/to/ConsoleAgents/ca.py rollback  # if ca is not on PATH
```

The fallback uses the same Python package, so Python and the CLI must still be able to start.
See [COMMANDS.md](COMMANDS.md#315-app-updates-and-rollback) for all options.

## Shut down when finished (`/swf`, `/qqq`)

It is **off whenever the shell starts**. `/swf` turns it on for this console session; the prompt then starts
with `⏻`. From then on **the console watches for "no tasks"**: as soon as no chat in any project is working
(checked every 3 s, also right after you turn it on; a chat is not working when it is done, stopped, or waiting
for your answer, while a chat waiting for usage limits still counts as working), aa:

1. turns off the screen of every Android device connected through `adb` (agents testing apps leave it on)
2. powers off this PC after 60 s (`run.shutdown_grace_s`), so you can still cancel

`/qqq` turns it off and also cancels a shutdown that is already counting down (on Windows, `shutdown /a`
works too). Closing the shell turns it off as well. `run.finish_lock_android` / `run.finish_shutdown` switch
the two actions on and off.

## When an agent hangs, goes silent or loops

* **Leftover processes no longer hang a turn.** Agents start helpers that outlive them (a Gradle daemon,
  the adb server, a dev server). Those used to keep the agent's output open, so ca waited forever for a
  turn that had already finished. Now a turn ends a few seconds after the agent itself exits.
* **Silent agent:** no output for `run.stall_timeout_min` (20) minutes → the turn is stopped and handed over.
* **Loop inside a turn:** the same action `run.loop_repeat_limit` (8) times in a row → stopped and handed over.
* **No progress across turns:** 3 turns in a row that finish nothing and report the same thing → another
  agent takes over and is told to try a different approach.
* **Crashed background process:** the chat says so and is marked `stopped`; a message or `/continue`
  restarts it. While you watch a quiet chat, a line says whether it is still working.
* **Stopping a chat** keeps the output of the turn it interrupted in the history, so the next agent knows
  what was already done (and committed).
* **Processes started by agents** are recorded while each turn runs. You see `⚙ started bash.exe (pid 4580): adb devices` in the
  chat; after the turn, `⚙ codex left 2 process(es) running` if any are still there. `ca ps` / `/ps` lists them
  (pid, chat, agent, age, memory, command) and `ca kill <pid>` / `ca kill all` / `/kill` stops them.
  `ca st set run.leftover_processes kill` stops them automatically after every turn.

## Projects, memory and settings

**Projects.** A project is a folder with a name. Its chats, roadmaps, logs and project memory are kept in
its `.autoagents/`, so each project has its own work. A folder becomes a project the first time you run a task
in it (named after the folder), or with `ca project add shop W:\code\shop -d "web shop"`.
Give a git address instead of a folder and it is cloned first: `ca project add https://github.com/me/shop.git`
clones into `./shop` and names the project `shop`; `ca project add https://github.com/me/shop.git W:\code` clones into
`W:\code\shop` (a new or empty folder is used as is); `ca project add web git@github.com:me/shop.git W:\code\web`
picks the name too. A folder that already is a clone of that repository is added without cloning again.
`ca project list` (`ca p`) shows them **numbered** (in the order they were added, so numbers do not move when
you use a project) with their chats. Everywhere a project is asked for, a number works like the name, and so does
a unique start of the name: `ca -P 2 status`, `ca s 2#3` (chat 3 of project 2), `/p 2` or `/p shop` in the shell
(switches project; the prompt shows `ca 2:shop>`), `/open 2#3`.
The shell opens the project you used last; `/project use shop` switches it, and the prompt shows its name.
`ca project remove` only removes the name from the list; files are never touched.

**Memory.** Facts that should outlive one task and one agent:

| | where | for |
|---|---|---|
| project memory (`p1`, `p2`…) | `<project>/.autoagents/memory.json` + `MEMORY.md` | build/test commands, conventions, pitfalls, decisions |
| global memory (`g1`, `g2`…) | `~/.autoagents/memory.json` + `MEMORY.md` | you and your machine: preferences, OS, tools; shared by every project |

Every new agent chat gets both (newest first, up to `run.memory_chars` / `run.global_memory_chars`); a
continued chat gets only entries added since its last turn. Agents add entries through the `remember`
field of their report (shown as `🧠 remembered ...`), and you manage them with `ca memory`. Starting a new
task keeps the memory.

**Settings.** Three layers: built-in defaults < global (`~/.autoagents/config.json`) < project
(`<project>/.autoagents/config.json`). `ca settings` lists every value and where it comes from;
`ca settings set run.max_turns 20` changes it for this project, `--global` for all projects.
Values are checked (`agents.codex.enabled false`, `agents.codex.levels '["light","mid"]'`, `balancer.soft_limit 0.7`).
Usage limits are always global, since they belong to your accounts.

## How it works

1. **Planning.** For a new task, the healthiest agent looks at the project and decides whether the task
   is complex. If it is, it writes a roadmap of 3-12 steps to `.autoagents/roadmap.json` and `ROADMAP.md`.
   It is shown every agent's current usage and strengths, so it can suggest an agent for each step and
   spread the load.
2. **Turns.** Each turn, one agent works on the current roadmap step. Its prompt contains the task, the
   roadmap, the progress log, recent conversation history and any messages from you. The agent ends with
   a JSON report: `status` (continue/done/blocked), `completed_steps`, `summary` and `next`.
   The roadmap is updated from that report.
3. **Balancing.** Before every turn, `score = strength × (1 − usage) × pace`:
   * **usage** is the fuller of the two windows (5-hour, weekly), so neither runs out
   * **pace** uses both windows together (`pace = 1 + 0.6 × weekly pace + 0.3 × 5-hour pace`, kept between
     0.5 and 1.6). A window's pace is the share of it already gone minus the share of the quota already used.
     An agent that has used 25% of its week with 61% of the week gone is behind: the quota it does not use
     before the reset is lost, so it is preferred (×1.28). One that spent 40% in the first day is spared.
     An agent not used at all this week gets a small bonus, enough to win new work but not to take a task
     from the agent already on it. `ca usage` shows each agent's pace; `balancer.pacing false` turns it off
   * an agent that is **running a turn in another chat** (any project) is not used at the same time: the next
     best agent takes the turn, or, when every suitable agent is busy, the chat waits until one is free. The
     shell shows them as `working (shop#2)`. `balancer.max_parallel` (default 1, or per agent
     `agents.<name>.max_parallel`) allows more, 0 = no limit
   * at or above the **hard limit** (95%) an agent is never started, so it does not run dry mid-step
   * above the **soft limit** (80%) it is used only as a last resort
   * the agent the roadmap suggested, and the agent already on the task, get a bonus
4. **Handover.** If an agent hits its limit (or crashes), it goes on cooldown until its reported reset
   time. The next agent's prompt says *"you are taking over from X"* and includes X's partial output.
   It also points to the full history in `.autoagents/TRANSCRIPT.md` and asks the agent to check the
   working tree before redoing anything. Moves made only to balance load get the same handover note.
5. **Agent conversations are kept, to save tokens.** Inside a chat, each agent keeps its own conversation (`claude --resume`,
   `codex exec resume`, `agy --conversation`). When the same agent works again, it gets a short prompt:
   only what other agents did since its last turn, your new messages, the roadmap and its next step.
   The full context (task, rules, up to `history_chars` of history) goes only to an agent starting a new
   chat. The planner's chat is reused for the first work turn too. After `session_max_turns` (8) turns
   a chat is replaced by a new one so its context stays small; if a chat cannot be reopened, a new
   one starts. `ca continue --fresh` makes every agent start over; `"resume_sessions": false` turns it off.
6. **All agents limited.** The relay waits with a countdown until the earliest reset (up to
   `max_wait_hours`), or stops with `--no-wait`. Run `ca continue` later.

### Where the usage numbers come from

| Agent | Source |
|---|---|
| Claude | account quota refresh through the usage-panel OAuth endpoint; also reports from Claude Code's `rate_limit_event` |
| Codex | account quota refresh through app-server; also reports from session rollout files (`~/.codex/sessions`) |
| Antigravity | built-in zero-turn `/usage` check (CLI 1.2.15+); selects the configured/default model's quota pool |
| Other providers or missing reports | local token estimates against configured/learned budgets; explicitly labeled `est` |

Usage data is global (`~/.autoagents/usage.json`), because limits belong to your account, not to a project.

Run `ca usage refresh` to force a new check, or `ca u refresh codex` for one agent. In the shell,
use `/usage refresh` or `/u refresh codex`. Checks do not send model prompts. Each agent reports
success or failure; a failure retains its previous snapshots and does not stop the remaining checks.
The command exits nonzero if any check fails. A partial result retains unmapped quota periods.

The table shows report age and the latest check status. `unknown` means no account data;
`0%` with a provider source is a measured zero. `est` is a local estimate, `stale` marks an expired
report, and `cached` marks a retained report after a failed check. `disabled` means the provider says
that quota window does not apply. Reported values include estimated local spend since observation.
`usage clear` and `usage reset` also discard refresh metadata. Manual `usage set` remains available.

Claude's usage endpoint is an internal client interface and can change; keychain-only logins are
unsupported. The installed Termux Debian/proot wrapper is supported. Missing or unsupported data
is never stored as a measured zero. Refreshing does not change the balancer's scoring rules.

## Files in your project (`.autoagents/`)

`chats/<id>/` one folder per chat: `task.json` (status, last agent, agent chat ids) · `roadmap.json` + `ROADMAP.md` ·
`steps.jsonl` + `STEPS.md` (one line per turn, limit, switch or wait) · `transcript.jsonl` + `TRANSCRIPT.md`
(all agent output) · `live.log` (console output) · `inbox.jsonl` (your queued messages) · `runner.json` (the
background process). Next to them: `chats.json` (current chat), `memory.json` + `MEMORY.md` (project memory) and
`config.json` (project settings). Older versions' `task.json` and `history/` are moved into `chats/` automatically.
Global: `~/.autoagents/` holds `usage.json`, `config.json`, `projects.json` and `memory.json`.

## Antigravity (Gemini on your Google account)

Log in once by running `agy` interactively. ca then runs it headless: the prompt is sent on stdin in its
NDJSON input mode, and `permission` maps to `--dangerously-skip-permissions`, `--mode accept-edits` or
`--mode plan`. Run `ca usage refresh` to fetch its current model quotas. Between reports, local token
spend is estimated. `ca usage set antigravity 60` can supply a manual value if quota checks are unavailable.

### Antigravity's Claude and GPT models: helpers for light steps

Antigravity also offers Claude and GPT models on your Google account. ca runs two of them as extra agents:

| agent | model | strength | takes |
|---|---|---|---|
| `antigravity` | Gemini 3.8 Flash (agy default) | 0.7 | any step |
| `agy-claude` | `claude-sonnet-5-5-medium` | 0.9 | light steps only |
| `agy-gpt` | `gpt-oss-120b-medium` | 0.6 | light steps only |

* The planner rates every roadmap step, and a simple task as a whole, as **light**, **mid** or **heavy**.
  It writes self-contained details for light/mid steps, so an agent with little history can do them.
* Helpers take only the levels in their `"levels"`. Heavy steps, unrated work and planning always go to
  `claude`, `codex` or `antigravity`. On light steps `agy-claude` usually leads, which saves your Claude
  and Codex plans for the hard parts.
* Helpers get a short history (`"history_chars": 8000` instead of 40000). They can still read
  `.autoagents/TRANSCRIPT.md` when they need more.
* Each helper has its own usage tracking. When its quota runs out it is paused until the reset, and the
  step goes to the next agent. With no helper free, the regular agents take light steps as well. Mid steps always go to `claude`, `codex` or `antigravity`.
* To use one only for a while, set `"active_until": "2026-12-31"`; it is skipped after that day.
  Use `"enabled": false` to switch one off, or change `"model"` (see `agy models`).
* `--agent agy-claude` forces a helper even on heavy or unrated work.

## Configuration

`ca config --global` writes `~/.autoagents/config.json`. A project's `.autoagents/config.json` overrides it.

```json
{
  "agents": {
    "claude": {"enabled": true, "model": null, "strength": 1.0, "budgets": {"five_hour": 4000000, "weekly": 40000000}},
    "antigravity": {"enabled": false},
    "mytool": {"type": "generic", "command": "mytool", "args": ["mytool", "--headless"], "description": "..."}
  },
  "balancer": {"soft_limit": 0.8, "hard_limit": 0.95},
  "run": {"max_turns": 40, "turn_timeout_min": 45, "permission": "full", "wait_on_limits": true, "planning": "auto"}
}
```

`permission: "full"` lets agents edit files and run commands without asking. That is what makes them
autonomous: it maps to `--dangerously-skip-permissions` (Claude, agy) and Codex's `--dangerously-bypass-approvals-and-sandbox`.
Use it only on projects you are willing to let them modify, preferably under git.

## Exit codes

`0` done · `2` turn limit · `3` no agent available · `4` an agent needs your answer
(`ca continue -m "..."`) · `5` repeated failures · `130` Ctrl+C.

## Tests

```
python -m unittest discover -s tests
```

The tests include an end-to-end relay that uses a fake agent CLI: it hits a limit, hands over, follows a roadmap, waits and resumes.
