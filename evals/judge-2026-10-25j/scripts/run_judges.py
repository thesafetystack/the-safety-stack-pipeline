"""Run the judges (Opus 5.5 and Sonnet 5.5 via claude CLI, Sol 6.1 via codex CLI) over the frozen task dirs, jailed.

    run_judges.py [--judges opus,sol,sonnet] [--sets ...] [--calls ...] [--limit N | --items id,id] [--parallel-opus 4]
                  [--parallel-sol 8] [--parallel-sonnet 4] [--timeout 3600] [--dry-run]
                  [--retry-failed/--no-retry-failed]

For every (set, item, call, judge) whose ``runs/<judge>/<set>/<item>[-<call>].json`` is not ``ok``: check that the task
dir holds exactly ``common.TASK_FILES[call]``, run the judge CLI inside the jail (``EVAL104_JAIL`` <task dir> <cmd>;
inside, cwd is the read-only task dir), save stdout to ``.raw`` and a record to ``.json``: ``label`` (the extracted
JSON object, schema-checked), ``ok``, ``error``, cost (claude ``total_cost_usd``; codex has none: tokens), tokens,
turns, wall time, attempts. Resumable: ``ok`` records are skipped; failed ones are retried unless
``--no-retry-failed``. Each run appends JSON lines to ``runs/run.log``.

* Retry: one more attempt (``MAX_ATTEMPTS`` = 2) on invalid JSON, a schema violation, a non-zero exit, a timeout or
  an error result.
* Usage limits (rate / usage-limit / overload messages in stderr or in the CLI's error fields, never in document
  text the judge read): the job is re-queued without spending an attempt and every worker of that judge waits
  (10 minutes, doubling to 2 hours; at most ``--max-limit-waits`` waits per job).
* The raw files hold the documents' text (codex echoes command output): they stay under EVAL104_PRIVATE; run
  ``secret_scan.py`` before copying anything derived from them into the repository.

Commands (verified 2026-10-25): claude -p --model claude-opus-5-5 --effort high --restricted --strict-mcp-config
--tools Read,Grep,Glob --no-session-persistence --output-format json <prompt>; codex exec --skip-git-repo-check
--ephemeral --ignore-user-config -m gpt-6.1-sol -c model_reasoning_effort="high" -s read-only -C /mnt/task --json
<prompt> (stdin closed: codex otherwise waits on it; the final message is the last ``agent_message`` event).
Sonnet (third judge, added 2026-10-26): the Opus command with ``--model claude-sonnet-5-5`` and nothing else changed
(same jail, task dirs, wrapper prompt, guides, retry and usage-limit rules).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C  # noqa: E402

MAX_ATTEMPTS = 2
LIMIT_WAIT0, LIMIT_WAIT_MAX = 600, 7200
LIMIT_RE = re.compile(r"usage limit|rate[ _-]?limit|hit your (usage )?limit|too many requests|exceeded your|quota "
                      r"exceeded|insufficient_quota|overloaded|credit balance is too low|\b429\b|\b529\b|try again "
                      r"(at|in|later)", re.I)
COMMANDS = {
    "opus": ["claude", "-p", "--model", "claude-opus-5-5", "--effort", "high", "--restricted", "--strict-mcp-config",
             "--tools", "Read,Grep,Glob", "--no-session-persistence", "--output-format", "json"],
    "sol": ["codex", "exec", "--skip-git-repo-check", "--ephemeral", "--ignore-user-config", "-m", "gpt-6.1-sol",
            "-c", 'model_reasoning_effort="high"', "-s", "read-only", "-C", "/mnt/task", "--json"],
}
COMMANDS["sonnet"] = [("claude-sonnet-5-5" if x == "claude-opus-5-5" else x) for x in COMMANDS["opus"]]
#: Judges run through the claude CLI (their stdout is parsed by :func:`parse_claude`); the others are codex.
CLAUDE_JUDGES = frozenset({"opus", "sonnet"})
_log_lock = threading.Lock()


def now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def log(entry: dict[str, Any]) -> None:
    p = C.private_root() / "runs" / "run.log"
    p.parent.mkdir(parents=True, exist_ok=True)
    with _log_lock, open(p, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": now(), **entry}, ensure_ascii=False) + "\n")


def extract_json(text: str | None) -> tuple[Any, str | None]:
    """The one JSON object of a final message: the whole text, else inside a code fence, else the outermost braces."""
    if not text or not text.strip():
        return None, "empty final message"
    t = text.strip()
    cands = [t]
    m = re.search(r"```(?:json)?\s*\n(.*?)\n```", t, re.S)
    if m:
        cands.append(m.group(1))
    if "{" in t and "}" in t:
        cands.append(t[t.index("{"):t.rindex("}") + 1])
    for c in cands:
        try:
            obj = json.loads(c)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj, None if c == t else "extracted from surrounding text"
    return None, "no JSON object in the final message"


def parse_claude(stdout: str) -> dict[str, Any]:
    try:
        d = json.loads(stdout)
    except json.JSONDecodeError:
        return {"message": None, "error": "stdout is not JSON", "limit_text": stdout[-2000:]}
    u = d.get("usage") or {}
    err = None
    if d.get("is_error") or d.get("subtype") != "success":
        err = f"claude error: subtype={d.get('subtype')} api_error_status={d.get('api_error_status')}"
    return {"message": d.get("result"), "error": err, "cost_usd": d.get("total_cost_usd"),
            "tokens": {"input": u.get("input_tokens"), "cache_read": u.get("cache_read_input_tokens"),
                       "cache_write": u.get("cache_creation_input_tokens"), "output": u.get("output_tokens")},
            "turns": d.get("num_turns"), "api_ms": d.get("duration_api_ms"),
            "limit_text": (str(d.get("result")) if err else "") + " " + str(d.get("api_error_status") or "")}


def parse_codex(stdout: str) -> dict[str, Any]:
    msg, usage, errors, turns = None, {}, [], 0
    for line in stdout.splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        typ = e.get("type")
        item = e.get("item") or {}
        if typ == "item.completed" and item.get("type") == "agent_message":
            msg = item.get("text")
        elif typ == "item.completed" and item.get("type") == "command_execution":
            turns += 1
        elif typ == "turn.completed":
            usage = e.get("usage") or {}
        elif typ in ("error", "turn.failed"):
            errors.append(json.dumps(e.get("error") or e.get("message") or e)[:1000])
    return {"message": msg, "error": ("codex error: " + " | ".join(errors)) if errors else None, "cost_usd": None,
            "tokens": {"input": usage.get("input_tokens"), "cache_read": usage.get("cached_input_tokens"),
                       "cache_write": usage.get("cache_write_input_tokens"), "output": usage.get("output_tokens"),
                       "reasoning": usage.get("reasoning_output_tokens")},
            "turns": turns, "limit_text": " ".join(errors)}


class Judge:
    """Per-judge state: the usage-limit pause shared by its workers."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.pause_until = 0.0
        self.wait = LIMIT_WAIT0
        self.lock = threading.Lock()

    def hold(self) -> None:
        while True:
            with self.lock:
                left = self.pause_until - time.time()
            if left <= 0:
                return
            time.sleep(min(left, 30))

    def limited(self) -> float:
        with self.lock:
            w = self.wait
            self.pause_until = max(self.pause_until, time.time() + w)
            self.wait = min(self.wait * 2, LIMIT_WAIT_MAX)
            return w

    def ok(self) -> None:
        with self.lock:
            self.wait = LIMIT_WAIT0


def run_cli(judge: str, task: Path, prompt: str, timeout: int) -> tuple[int | None, str, str, float]:
    cmd = [str(C.env_path("EVAL104_JAIL")), str(task), *COMMANDS[judge], prompt]
    t0 = time.time()
    p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                         cwd=str(C.private_root()), start_new_session=True)
    try:
        out, err = p.communicate(timeout=timeout)
        rc: int | None = p.returncode
    except subprocess.TimeoutExpired:
        os.killpg(p.pid, signal.SIGTERM)
        try:
            out, err = p.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGKILL)  # the jailed CLI ignored SIGTERM for 60 s
            out, err = p.communicate()
        rc = None
    return rc, out or "", err or "", time.time() - t0


def validate(obj: Any, schema: dict[str, Any]) -> list[str]:
    import jsonschema

    v = jsonschema.Draft202012Validator(schema)
    return [f"{'/'.join(map(str, e.absolute_path))}: {e.message[:200]}" for e in v.iter_errors(obj)][:20]


def do_job(j: Judge, set_name: str, item_id: str, call: str, timeout: int, max_limit_waits: int) -> dict[str, Any]:
    raw_p, json_p = C.run_paths(j.name, set_name, item_id, call)
    raw_p.parent.mkdir(parents=True, exist_ok=True)
    task = C.task_dir(set_name, item_id, call)
    have = sorted(os.listdir(task)) if task.is_dir() else []
    if have != sorted(C.TASK_FILES[call]):
        rec = {"ok": False, "error": f"task dir files {have} != {sorted(C.TASK_FILES[call])}", "attempts": 0}
        json_p.write_text(C.dumps(rec), "utf-8")
        return rec
    schema = json.loads((task / "schema.json").read_text("utf-8"))
    prompt = C.wrapper_prompt(call)
    attempts, waits, history = 0, 0, []
    rec: dict[str, Any] = {}
    while attempts < MAX_ATTEMPTS:
        j.hold()
        started = now()
        rc, out, err, wall = run_cli(j.name, task, prompt, timeout)
        parsed = parse_claude(out) if j.name in CLAUDE_JUDGES else parse_codex(out)
        limit_hay = (err[-4000:] + " " + (parsed.get("limit_text") or "")) if (rc != 0 or parsed["error"]
                                                                             or not parsed["message"]) else ""
        if limit_hay and LIMIT_RE.search(limit_hay) and waits < max_limit_waits:
            w = j.limited()
            waits += 1
            log({"judge": j.name, "set": set_name, "item": item_id, "call": call, "event": "usage_limit",
                 "wait_s": w, "rc": rc, "stderr_tail": err[-300:]})
            continue
        attempts += 1
        raw_p.write_text(out, "utf-8")
        label, note = extract_json(parsed["message"])
        problems = []
        if rc is None:
            problems.append(f"timeout after {timeout} s")
        elif rc != 0:
            problems.append(f"exit code {rc}")
        if parsed["error"]:
            problems.append(parsed["error"])
        if label is None:
            problems.append(note or "no label")
        schema_errors = validate(label, schema) if label is not None else []
        if schema_errors:
            problems.append(f"schema: {schema_errors[0]}" + (f" (+{len(schema_errors) - 1} more)"
                                                              if len(schema_errors) > 1 else ""))
        rec = {"judge": j.name, "set": set_name, "item_id": item_id, "call": call, "ok": not problems,
               "error": "; ".join(problems) or None, "extract_note": note if label is not None else None,
               "schema_errors": schema_errors, "label": label, "cost_usd": parsed.get("cost_usd"),
               "tokens": parsed.get("tokens"), "turns": parsed.get("turns"), "wall_s": round(wall, 1),
               "api_ms": parsed.get("api_ms"), "rc": rc, "started": started, "finished": now(),
               "attempts": attempts, "limit_waits": waits, "stderr_tail": err[-1500:] if problems else "",
               "prompt_sha256": C.sha256_text(prompt), "history": history}
        log({k: rec[k] for k in ("judge", "set", "item_id", "call", "ok", "error", "cost_usd", "wall_s", "attempts")}
            | {"tokens": rec["tokens"]})
        if rec["ok"]:
            j.ok()
            break
        history.append({k: rec[k] for k in ("error", "cost_usd", "tokens", "wall_s", "started")})
    json_p.write_text(C.dumps(rec), "utf-8")
    return rec


def jobs(a: argparse.Namespace) -> list[tuple[str, str, str, str]]:
    out = []
    want_items = set(a.items.split(",")) if a.items else None
    calls = set(a.calls.split(",")) if a.calls else None
    for set_name in a.sets.split(","):
        items = [r["item_id"] for r in C.read_jsonl(C.SET_ROOT / set_name / "items.jsonl")]
        items = [i for i in items if want_items is None or i in want_items]
        if a.limit:
            items = items[:a.limit]
        for judge in a.judges.split(","):
            for item in items:
                for call in C.CALLS[set_name]:
                    if calls and call not in calls:
                        continue
                    _, json_p = C.run_paths(judge, set_name, item, call)
                    if json_p.exists():
                        prev = json.loads(json_p.read_text("utf-8"))
                        if prev.get("ok") or not a.retry_failed:
                            continue
                    out.append((judge, set_name, item, call))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--judges", default=",".join(C.JUDGES))
    ap.add_argument("--sets", default=",".join(C.SETS))
    ap.add_argument("--calls", default="")
    ap.add_argument("--items", default="")
    ap.add_argument("--limit", type=int, default=0, help="first N items of each set (item-id order)")
    ap.add_argument("--parallel-opus", type=int, default=4)
    ap.add_argument("--parallel-sol", type=int, default=8)
    ap.add_argument("--parallel-sonnet", type=int, default=4)
    ap.add_argument("--timeout", type=int, default=3600)
    ap.add_argument("--max-limit-waits", type=int, default=12)
    ap.add_argument("--retry-failed", action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    todo = jobs(a)
    print(f"{len(todo)} jobs: " + json.dumps({j: sum(t[0] == j for t in todo) for j in a.judges.split(",")}))
    if a.dry_run:
        for t in todo[:50]:
            print(" ", *t)
        return 0
    log({"event": "start", "jobs": len(todo), "argv": sys.argv[1:]})
    judges = {n: Judge(n) for n in a.judges.split(",")}
    par = {"opus": a.parallel_opus, "sol": a.parallel_sol, "sonnet": a.parallel_sonnet}
    pools = {n: ThreadPoolExecutor(max_workers=par[n], thread_name_prefix=n) for n in judges}
    futs = [pools[j].submit(do_job, judges[j], s, i, c, a.timeout, a.max_limit_waits) for j, s, i, c in todo]
    n_ok = n_bad = 0
    for f in futs:
        try:
            r = f.result()
        except Exception as exc:  # a bug in this runner: report loudly, keep the other jobs going
            n_bad += 1
            print("RUNNER ERROR:", repr(exc), file=sys.stderr)
            continue
        if r.get("ok"):
            n_ok += 1
        else:
            n_bad += 1
            print(f"FAILED {r.get('judge')} {r.get('set')} {r.get('item_id')} {r.get('call')}: {r.get('error')}",
                  file=sys.stderr)
    for p in pools.values():
        p.shutdown()
    log({"event": "end", "ok": n_ok, "failed": n_bad})
    print(f"done: {n_ok} ok, {n_bad} failed")
    return 0 if n_bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
