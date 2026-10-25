"""A stand-in for an AI CLI, used by the integration tests through the "generic" provider.

usage: fake_agent.py <name> <behaviour>
  limit  -> prints partial work, then a usage-limit error, exit 1
  work   -> plans when asked; otherwise completes one roadmap step per turn and says done at the end
  badresume -> like work, but fails at once when asked to resume a conversation
Extra args `--resume <id>` continue a conversation; every run prints "SESSION: <id>".
Every prompt it receives is appended to $FAKE_AGENT_LOG so tests can inspect what was sent.
"""
import json
import os
import re
import sys

name, behaviour = sys.argv[1], sys.argv[2]
resume = sys.argv[sys.argv.index("--resume") + 1] if "--resume" in sys.argv else None
prompt = sys.stdin.read()
with open(os.environ["FAKE_AGENT_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps({"agent": name, "prompt": prompt, "resume": resume}) + "\n")

if resume and behaviour == "badresume":
    print(f"No conversation found with session ID: {resume}")
    sys.exit(1)
print(f"SESSION: {resume or name + '-' + os.urandom(3).hex()}")

if behaviour == "daemon":  # like Codex starting a Gradle daemon: a child keeps our stdout open
    import subprocess
    import tempfile
    subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], cwd=tempfile.gettempdir())
    report = {"status": "done", "completed_steps": [], "summary": "built it", "next": "", "add_steps": []}
    print(f"Did the work.\n<autoagents-report>\n{json.dumps(report)}\n</autoagents-report>", flush=True)
    sys.exit(0)

if behaviour == "hang":  # prints a little, then goes silent forever
    import time
    print("starting a long command...", flush=True)
    time.sleep(120)
    sys.exit(0)

if behaviour == "loop":  # repeats the same action over and over
    import time
    for _ in range(200):
        print("[tool] shell: adb shell input tap 100 200", flush=True)
        time.sleep(0.02)
    sys.exit(0)

if behaviour == "limit":
    print("Working on it... created src/half_done.py")
    print("You've hit your usage limit. Try again in 2 hours 30 minutes.")
    sys.exit(1)

if "PLANNER" in prompt:
    plan = {"complex": True, "reason": "several stages",
            "roadmap": [{"title": "Create module", "agent": "codex", "level": "mid"},
                        {"title": "Write tests", "agent": None, "level": "heavy"}]}
    print(f"Plan ready.\n<autoagents-plan>\n{json.dumps(plan)}\n</autoagents-plan>")
    sys.exit(0)

m = re.search(r"Work on roadmap step (\d+)", prompt)
step = int(m.group(1)) if m else None
total = len(re.findall(r"^\[[ x~]\] \d+\.", prompt, re.M))
done = step is None or step >= total
report = {"status": "done" if done else "continue", "completed_steps": [step] if step else [],
          "summary": f"{name} finished step {step}", "next": "", "add_steps": []}
if behaviour == "spin":  # keeps "working" without getting anywhere
    report.update(status="continue", completed_steps=[], summary="Still investigating the build error.")
if behaviour == "weak":  # always finds its work too hard
    report.update(status="continue", completed_steps=[], too_hard=True, summary=f"{name}: too hard for me")
if os.environ.get("FAKE_PLANNER") == name and "Work on roadmap step" not in prompt:  # plans mid-work
    report.update(status="done", add_steps=json.loads(os.environ["FAKE_STEPS"]))
if os.environ.get("FAKE_DELEGATOR") == name and "Work on roadmap step" not in prompt:
    report["delegate"] = json.loads(os.environ["FAKE_DELEGATE"])
    report["commits"] = ["abc1234 Add the parser"]
if os.environ.get("FAKE_REMEMBER"):  # JSON list of {"scope", "text"} to put in the report
    report["remember"] = json.loads(os.environ["FAKE_REMEMBER"])
print(f"Did the work.\n<autoagents-report>\n{json.dumps(report)}\n</autoagents-report>")
