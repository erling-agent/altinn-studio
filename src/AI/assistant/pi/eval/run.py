"""Run the spike's tasks on pi or on the assistant service, and score the apps they build.

Each run starts from an app template or a seed app in this folder, so it needs no Gitea or
Langfuse. The assistant runs as its own service per run, with a file-based Git remote in place of
Gitea. Both harnesses get the same model, skills and Altinn tools, and generate data models with
Designer's endpoint on dev.

    python pi/eval/run.py --harness pi --out pi/runs/pi
    python pi/eval/run.py --harness assistant --out pi/runs/assistant --task v8-barn --reps 3
    python pi/eval/run.py --report pi/runs/pi pi/runs/assistant
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

import httpx

HERE = Path(__file__).resolve().parent
PI_DIR = HERE.parent
ASSISTANT = PI_DIR.parent
REPO_ROOT = ASSISTANT.parents[2]
TEMPLATES = REPO_ROOT / "src" / "App" / "template"
FIXTURES = HERE / "fixtures"
SKILLS = ASSISTANT / "agents" / "skills"
PYTHON = ASSISTANT / ".venv" / "bin" / "python"
sys.path.insert(0, str(ASSISTANT))
sys.path.insert(0, str(HERE))

from benchmarks.app_model import load_app  # noqa: E402
from benchmarks.evaluators import evaluate  # noqa: E402
from checks import CHECKS  # noqa: E402

AZURE_KEY = os.environ.get("AZURE_OPENAI_API_KEY", "")
# Data model generation runs Designer's code on dev, the same for both harnesses. The endpoint
# reads the files from the request and writes nothing. The app only authorizes the call.
DESIGNER = {
    "DESIGNER_BASE_URL": "https://dev.altinn.studio",
    "STUDIO_REPO_OWNER": "ttd",
    "STUDIO_APP_NAME": "test-erling",
    "STUDIO_DESIGNER_API_KEY": os.environ.get("STUDIO_DEV_API_KEY", ""),
}
os.environ.update(DESIGNER)
MODEL = "gpt-6.1-terra"
# USD per million tokens. pi's catalog has no price for gpt-6.1-terra, so both sides use the
# gpt-5.6-terra price. The comparison holds; the absolute cost is an estimate.
PRICES = {
    "gpt-6.1-terra": (2.0, 12.0, 0.2, 2.5),
    "gpt-6.1-sol": (2.0, 10.0, 0.1, 2.5),
    "gpt-6-luna": (0.1, 0.5, 0.01, 0.125),
}
RUN_TIMEOUT = 1800
GIT_ID = ["-c", "user.name=Studio", "-c", "user.email=studio@localhost"]


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *GIT_ID, *args], check=True, capture_output=True, text=True).stdout


def prepare_app(task: dict, dest: Path) -> str:
    source = FIXTURES / task["seed"] if task.get("seed") else TEMPLATES / task["version"] / "src"
    shutil.copytree(source, dest)
    git(dest, "init", "-q", "-b", "main")
    git(dest, "add", "-A")
    git(dest, "commit", "-qm", "Start")
    return git(dest, "rev-parse", "HEAD").strip()


def goals(task: dict) -> list[str]:
    return task.get("goals") or [task["goal"]]


def changed_files(app: Path, base: str) -> list[str]:
    tracked = git(app, "diff", "--name-only", base).split()
    untracked = git(app, "ls-files", "--others", "--exclude-standard").split()
    return sorted(set(tracked) | set(untracked))


def reformat_json(app: Path, files: list[str]) -> list[str]:
    """Write changed JSON files as Studio does. This is the host's step after the turn."""
    reformatted = []
    for rel in files:
        path = app / rel
        if path.suffix != ".json" or not path.exists():
            continue
        text = path.read_text(encoding="utf-8-sig")
        try:
            formatted = json.dumps(json.loads(text), indent=2, ensure_ascii=False) + "\n"
        except json.JSONDecodeError:
            continue
        if formatted.strip() != text.strip():
            path.write_text(formatted, encoding="utf-8")
            reformatted.append(rel)
    return reformatted


def verify(app: Path, base: str) -> dict:
    out = subprocess.run(
        [str(PYTHON), str(PI_DIR / "studio_tool.py"), "verify_changes", "--repo", str(app), "--base", base],
        capture_output=True,
        text=True,
    ).stdout
    result = json.loads(out)
    nothing = result["content"].startswith("No changed files")
    return {"passed": not result["is_error"] or nothing, "detail": result["content"][:4000]}


def score(app: Path, task: dict) -> dict:
    model = load_app(app)
    scores: dict[str, Any] = {}
    if task.get("rubric"):
        for s in evaluate(model, task["rubric"]):
            scores[s.name] = {"value": s.value, "comment": s.comment}
    for name in task.get("checks", []):
        try:
            passed, comment = CHECKS[name](app, model, task)
        except Exception as exc:
            passed, comment = False, f"check crashed: {type(exc).__name__}: {exc}"
        scores[name] = {"value": 1.0 if passed else 0.0, "comment": comment}
    values = [s["value"] for s in scores.values()]
    return {"scores": scores, "score": round(sum(values) / len(values), 3) if values else 0.0}


def cost(model: str, input_: int, output: int, cache_read: int, cache_write: int) -> float:
    p = PRICES.get(model, PRICES[MODEL])
    return (input_ * p[0] + output * p[1] + cache_read * p[2] + cache_write * p[3]) / 1_000_000


# ---------------------------------------------------------------------------------------------
# pi
# ---------------------------------------------------------------------------------------------


def run_pi(task: dict, workdir: Path) -> dict:
    app = workdir / "app"
    base = prepare_app(task, app)
    agent_dir = workdir / "pi-agent"
    agent_dir.mkdir()
    shutil.copy(PI_DIR / "agent" / "models.json", agent_dir / "models.json")
    attachment_args = prepare_pi_attachments(task, workdir)
    chat = task.get("mode") == "chat"
    env = {
        **os.environ,
        "PI_CODING_AGENT_DIR": str(agent_dir),
        "PI_OFFLINE": "1",
        "PI_TELEMETRY": "0",
        "PI_SKIP_VERSION_CHECK": "1",
        "AZURE_OPENAI_API_KEY": AZURE_KEY,
        "STUDIO_PYTHON": str(PYTHON),
        "STUDIO_TOOL": str(PI_DIR / "studio_tool.py"),
        "STUDIO_SKILLS_DIR": str(SKILLS),
        "STUDIO_ATTACHMENTS_DIR": str(workdir / "attachments"),
        "STUDIO_EVENTS": str(workdir / "gate.jsonl"),
    }
    started = time.monotonic()
    exit_codes: list[Any] = []
    turn_base = base
    reformatted: list[str] = []
    for n, goal in enumerate(goals(task), start=1):
        cmd = [
            "pi", "--mode", "json", "-p",
            "--session-dir", str(workdir / "session"),
            "--no-extensions", "-e", str(PI_DIR / "extension" / "index.ts"),
            "--no-skills", "--skill", str(SKILLS),
            "--no-prompt-templates", "--no-themes",
            "--model", f"azure/{MODEL}", "--thinking", "off",
            "--append-system-prompt", str(PI_DIR / "APPEND_SYSTEM.md"),
        ]  # fmt: skip
        if chat:
            cmd += ["--append-system-prompt", str(PI_DIR / "APPEND_CHAT.md")]
            cmd += ["--tools", "read,grep,find,ls,altinn_layout_props,altinn_policy_subjects"]
        if n > 1:
            cmd.append("--continue")
        cmd += [*(attachment_args if n == 1 else []), goal]
        with (workdir / f"events-{n}.jsonl").open("w") as out, (workdir / f"stderr-{n}.log").open("w") as err:
            try:
                # pi reads a piped stdin as more input in print mode, so it must be closed.
                proc = subprocess.run(
                    cmd,
                    cwd=app,
                    env={**env, "STUDIO_START_COMMIT": turn_base},
                    stdin=subprocess.DEVNULL,
                    stdout=out,
                    stderr=err,
                    timeout=RUN_TIMEOUT,
                )
                exit_codes.append(proc.returncode)
            except subprocess.TimeoutExpired:
                exit_codes.append("timeout")
        # Studio's step after each turn: format the changed JSON files and commit them.
        turn_files = changed_files(app, turn_base)
        reformatted += reformat_json(app, turn_files)
        if turn_files:
            git(app, "add", "-A")
            git(app, "commit", "-qm", f"Studio Assistant, turn {n}")
            turn_base = git(app, "rev-parse", "HEAD").strip()
    seconds = round(time.monotonic() - started, 1)

    metrics = merge_metrics([parse_pi_events(workdir / f"events-{n}.jsonl") for n in range(1, len(goals(task)) + 1)])
    gate_file = workdir / "gate.jsonl"
    gate = [json.loads(line) for line in gate_file.read_text().splitlines()] if gate_file.exists() else []
    files = changed_files(app, base)
    return {
        "exit_code": exit_codes,
        "seconds": seconds,
        "files": files,
        "json_reformatted": reformatted,
        "verify": verify(app, base),
        "gate": gate,
        **metrics,
        **score(app, {**task, "_base": base, "_files": files, "_final_text": metrics["final_text"]}),
    }


def prepare_pi_attachments(task: dict, workdir: Path) -> list[str]:
    """Give pi each PDF as page images and extracted text, as a sandbox with poppler can."""
    folder = workdir / "attachments"
    folder.mkdir()
    args = []
    for name in task.get("attachments", []):
        pdf = folder / name
        shutil.copy(FIXTURES / name, pdf)
        subprocess.run(["pdftoppm", "-png", "-r", "110", str(pdf), str(folder / pdf.stem)], check=True)
        subprocess.run(["pdftotext", "-layout", str(pdf), str(folder / f"{pdf.stem}.txt")], check=True)
        args += [f"@{p}" for p in sorted(folder.glob(f"{pdf.stem}*.png"))] + [f"@{folder / f'{pdf.stem}.txt'}"]
    return args


def parse_pi_events(path: Path) -> dict:
    turns = 0
    tools: Counter[str] = Counter()
    tool_errors = 0
    usage: Counter[str] = Counter()
    final_text = ""
    skill_reads: list[str] = []
    errors: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = event.get("type")
        if kind == "turn_start":
            turns += 1
        elif kind == "tool_execution_start":
            tools[event["toolName"]] += 1
            arg_path = str((event.get("args") or {}).get("path", ""))
            if event["toolName"] == "read" and str(SKILLS) in arg_path:
                skill_reads.append(arg_path.replace(str(SKILLS) + "/", ""))
        elif kind == "tool_execution_end" and event.get("isError"):
            tool_errors += 1
        elif kind == "message_end" and (message := event.get("message") or {}).get("role") == "assistant":
            u = message.get("usage") or {}
            for key in ("input", "output", "cacheRead", "cacheWrite"):
                usage[key] += int(u.get(key) or 0)
            text = "".join(c.get("text", "") for c in message.get("content") or [] if c.get("type") == "text")
            if text.strip():
                final_text = text
            if message.get("stopReason") == "error":
                errors.append(str(message.get("errorMessage"))[:500])
    return {
        "turns": turns,
        "model_calls": turns,
        "tool_calls": dict(tools),
        "tool_errors": tool_errors,
        "usage": dict(usage),
        "cost_usd": round(cost(MODEL, usage["input"], usage["output"], usage["cacheRead"], usage["cacheWrite"]), 4),
        "skill_reads": skill_reads,
        "provider_errors": errors,
        "final_text": final_text,
    }


def merge_metrics(parts: list[dict]) -> dict:
    merged: dict[str, Any] = {"turns": 0, "model_calls": 0, "tool_errors": 0, "cost_usd": 0.0}
    tools: Counter[str] = Counter()
    usage: Counter[str] = Counter()
    for part in parts:
        for key in ("turns", "model_calls", "tool_errors"):
            merged[key] += part[key]
        merged["cost_usd"] = round(merged["cost_usd"] + part["cost_usd"], 4)
        tools.update(part["tool_calls"])
        usage.update(part["usage"])
    merged.update(
        tool_calls=dict(tools),
        usage=dict(usage),
        skill_reads=[r for p in parts for r in p["skill_reads"]],
        provider_errors=[e for p in parts for e in p["provider_errors"]],
        final_text=parts[-1]["final_text"] if parts else "",
        final_texts=[p["final_text"] for p in parts],
    )
    return merged


# ---------------------------------------------------------------------------------------------
# The assistant service
# ---------------------------------------------------------------------------------------------


def run_assistant(task: dict, workdir: Path, port: int) -> dict:
    seed = workdir / "seed"
    base = prepare_app(task, seed)
    owner, app_name = DESIGNER["STUDIO_REPO_OWNER"], DESIGNER["STUDIO_APP_NAME"]
    remote_root = workdir / "remote"
    bare = remote_root / owner / f"{app_name}.git"
    bare.parent.mkdir(parents=True)
    subprocess.run(["git", "clone", "-q", "--bare", str(seed), str(bare)], check=True)

    log = workdir / "assistant.jsonl"
    env = {
        **os.environ,
        "SPIKE_LOG": str(log),
        "AZURE_API_KEY": AZURE_KEY,
        "LANGFUSE_ENABLED": "false",
        "GITEA_BASE_URL": f"file://{remote_root}",
        "AGENT_ATTACHMENTS_PATH": str(workdir / "attachments"),
        "TMPDIR": str(workdir / "tmp"),
    }
    (workdir / "tmp").mkdir()
    server = subprocess.Popen(
        [str(PYTHON), str(HERE / "assistant_server.py"), str(port)],
        env=env,
        stdout=(workdir / "server.log").open("w"),
        stderr=subprocess.STDOUT,
    )
    base_url = f"http://127.0.0.1:{port}"
    session_id = str(uuid.uuid4())
    branch = f"assistant_{session_id[:8]}"
    statuses: list[Any] = []
    started = time.monotonic()
    try:
        for _ in range(120):
            try:
                if httpx.get(f"{base_url}/health", timeout=2).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        started = time.monotonic()
        for n, goal in enumerate(goals(task), start=1):
            body: dict[str, Any] = {
                "session_id": session_id,
                "goal": goal,
                "repo_url": f"http://studio.localhost/{owner}/{app_name}.git",
                "cost_org": owner,
                "allow_app_changes": task.get("mode") != "chat",
                "attachments": assistant_attachments(task) if n == 1 else [],
            }
            if n > 1 and git(bare, "branch", "--list", branch).strip():
                body["branch"] = branch
            turn_started = time.time()
            response = httpx.post(
                f"{base_url}/api/agent/start",
                headers={"X-Api-Key": DESIGNER["STUDIO_DESIGNER_API_KEY"], "X-Developer": "spike"},
                json=body,
                timeout=120,
            )
            response.raise_for_status()
            statuses.append(await_turn(base_url, session_id, log, turn_started, started))
        seconds = round(time.monotonic() - started, 1)
    finally:
        server.terminate()
        server.wait(timeout=30)

    app = workdir / "app"
    pushed = bool(git(bare, "branch", "--list", branch).strip())
    subprocess.run(["git", "clone", "-q", str(bare), str(app)], check=True)
    if pushed:
        git(app, "checkout", "-q", branch)
    files = changed_files(app, base)
    metrics = parse_assistant_log(log)
    return {
        "exit_code": statuses,
        "seconds": seconds,
        "branch_pushed": pushed,
        "files": files,
        "json_reformatted": reformat_json(app, files),
        "verify": verify(app, base),
        **metrics,
        **score(app, {**task, "_base": base, "_files": files, "_final_text": metrics.get("final_text", "")}),
    }


def await_turn(base_url: str, session_id: str, log: Path, turn_started: float, started: float) -> str:
    """Wait for the turn's terminal event in the log, since the status of the session stays done."""
    while time.monotonic() - started < RUN_TIMEOUT:
        time.sleep(2)
        if not log.exists():
            continue
        for line in log.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            if record["kind"] != "event" or record["at"] < turn_started:
                continue
            kind = (record["event"] or {}).get("type")
            if kind in ("done", "error"):
                time.sleep(1)
                return str(httpx.get(f"{base_url}/api/agent/status/{session_id}", timeout=30).json().get("status"))
    return "timeout"


def assistant_attachments(task: dict) -> list[dict]:
    out = []
    for name in task.get("attachments", []):
        data = (FIXTURES / name).read_bytes()
        out.append(
            {
                "name": name,
                "mimeType": "application/pdf",
                "size": len(data),
                "dataBase64": base64.b64encode(data).decode(),
            }
        )
    return out


def parse_assistant_log(path: Path) -> dict:
    actor_calls = 0
    usage: Counter[str] = Counter()
    clients: Counter[str] = Counter()
    statuses: Counter[str] = Counter()
    final_texts: list[str] = []
    total_cost = 0.0
    if not path.exists():
        return {}
    for line in path.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        if record["kind"] == "actor_call":
            actor_calls += 1
            # The adapter's input_tokens includes the cached tokens, as Azure reports them.
            u = record.get("usage") or {}
            read = int(u.get("cache_read_input_tokens") or 0)
            write = int(u.get("cache_creation_input_tokens") or 0)
            fresh = int(u.get("input_tokens") or 0) - read - write
            out = int(u.get("output_tokens") or 0)
            usage.update({"input": fresh, "output": out, "cacheRead": read, "cacheWrite": write})
            total_cost += cost(record.get("model") or MODEL, fresh, out, read, write)
        elif record["kind"] == "client_call":
            clients[f"{record.get('role')}:{record.get('model')}"] += 1
        elif record["kind"] == "event":
            event = record["event"] or {}
            statuses[str(event.get("type"))] += 1
            if event.get("type") == "assistant_message":
                data = event.get("data") or {}
                final_texts.append(str(data.get("content") or data.get("message") or data)[:6000])
    return {
        "turns": actor_calls,
        "model_calls": actor_calls + sum(clients.values()),
        "client_calls": dict(clients),
        "event_types": dict(statuses),
        "usage": dict(usage),
        "cost_usd": round(total_cost, 4),
        "final_text": final_texts[-1] if final_texts else "",
        "final_texts": final_texts,
    }


# ---------------------------------------------------------------------------------------------


def run_one(harness: str, task: dict, out: Path, rep: int, port: int) -> dict:
    workdir = out / f"{task['id']}-{rep}"
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True)
    try:
        result = run_pi(task, workdir) if harness == "pi" else run_assistant(task, workdir, port)
    except Exception as exc:
        result = {"crash": f"{type(exc).__name__}: {exc}", "score": 0.0}
    result = {"harness": harness, "task": task["id"], "rep": rep, **result}
    (workdir / "result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False))
    print(
        f"{harness:9} {task['id']:15} rep {rep}: score {result.get('score')} in {result.get('seconds')} s", flush=True
    )
    return result


def wait_for_headroom(minimum: int) -> None:
    """Start a run only when the deployment has room, so the eval does not throttle other users."""
    while True:
        response = httpx.post(
            "https://team-altinnstudio-ai.openai.azure.com/openai/v1/responses",
            headers={"api-key": AZURE_KEY},
            json={"model": MODEL, "input": "ok", "reasoning": {"effort": "none"}, "max_output_tokens": 16},
            timeout=60,
        )
        remaining = int(response.headers.get("x-ratelimit-remaining-tokens", "0"))
        if response.status_code == 200 and remaining >= minimum:
            return
        time.sleep(10)


def rescore(dirs: list[Path]) -> None:
    """Score finished runs again from their apps, after a change to a check."""
    for d in dirs:
        for f in sorted(d.glob("*/result.json")):
            result = json.loads(f.read_text())
            app = f.parent / "app"
            if "crash" in result or not app.exists():
                continue
            task = next(t for t in json.loads((HERE / "tasks.json").read_text()) if t["id"] == result["task"])
            base = git(app, "rev-list", "--max-parents=0", "HEAD").split()[0]
            files = changed_files(app, base)
            result.update(
                score(app, {**task, "_base": base, "_files": files, "_final_text": result.get("final_text", "")})
            )
            f.write_text(json.dumps(result, indent=2, ensure_ascii=False))


def report(dirs: list[Path]) -> None:
    rows = []
    for d in dirs:
        for f in sorted(d.glob("*/result.json")):
            rows.append(json.loads(f.read_text()))
    print("| harness | task | rep | score | verify | turns | model calls | input+cache | output | cost $ | s |")
    print("|---|---|---|---|---|---|---|---|---|---|---|")
    for r in sorted(rows, key=lambda r: (r["task"], r["harness"], r["rep"])):
        u = r.get("usage") or {}
        print(
            f"| {r['harness']} | {r['task']} | {r['rep']} | {r.get('score')} | "
            f"{(r.get('verify') or {}).get('passed')} | {r.get('turns')} | {r.get('model_calls')} | "
            f"{u.get('input', 0) + u.get('cacheRead', 0) + u.get('cacheWrite', 0)} | {u.get('output')} | "
            f"{r.get('cost_usd')} | {r.get('seconds')} |"
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--harness", choices=["pi", "assistant", "both"])
    parser.add_argument("--headroom", type=int, default=0, help="tokens per minute to leave free before each run")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--task", action="append")
    parser.add_argument("--reps", type=int, default=1)
    parser.add_argument("--rep-from", type=int, default=1)
    parser.add_argument("--parallel", type=int, default=4)
    parser.add_argument("--port", type=int, default=18100)
    parser.add_argument("--report", nargs="*", type=Path)
    parser.add_argument("--rescore", nargs="*", type=Path)
    ns = parser.parse_args()
    if ns.rescore is not None:
        rescore(ns.rescore)
        report(ns.rescore)
        return
    if ns.report is not None:
        report(ns.report)
        return
    if not AZURE_KEY:
        sys.exit("AZURE_OPENAI_API_KEY is not set")
    tasks = [t for t in json.loads((HERE / "tasks.json").read_text()) if not ns.task or t["id"] in ns.task]
    jobs = [(task, rep) for rep in range(ns.rep_from, ns.reps + 1) for task in tasks]
    ns.out.mkdir(parents=True, exist_ok=True)
    if ns.harness == "both":
        # One run at a time, alternating the harness that goes first, so load and time of day hit both alike.
        for i, (task, rep) in enumerate(jobs):
            order = ["pi", "assistant"] if i % 2 == 0 else ["assistant", "pi"]
            for harness in order:
                if ns.headroom:
                    wait_for_headroom(ns.headroom)
                run_one(harness, task, (ns.out / harness).resolve(), rep, ns.port)
        report([ns.out / "pi", ns.out / "assistant"])
        return
    with concurrent.futures.ThreadPoolExecutor(ns.parallel) as pool:
        futures = [
            pool.submit(run_one, ns.harness, task, ns.out.resolve(), rep, ns.port + i)
            for i, (task, rep) in enumerate(jobs)
        ]
        for future in futures:
            future.result()
    report([ns.out])


if __name__ == "__main__":
    main()
