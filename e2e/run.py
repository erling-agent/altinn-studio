"""Runs the same jobs through the assistant's `Runner` against a running assistant runner, and writes the results.

Run it from `src/AI/assistant` of the branch under test, with ASSISTANT_RUNNER_URL set:

    uv run --no-sync python run.py <templates folder> <results file> <cases...>
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

from agents.core import runner
from agents.core.tools.upgrade_app_tool import _apply_changes

_HOSTILE_TARGET = (
    '  <Target Name="CallHome" BeforeTargets="Build">\n'
    '    <Exec Command="curl --fail --silent --show-error --max-time 20 https://example.com" />\n'
    "  </Target>\n"
)


def make_repo(template: Path, folder: Path) -> str:
    shutil.copytree(template, folder)
    for args in (["init", "-q"], ["add", "-A"], ["-c", "user.name=e2e", "-c", "user.email=e2e@example.com", "commit", "-qm", "base"]):
        subprocess.run(["git", *args], cwd=folder, check=True)
    return str(folder)


async def timed(job) -> tuple[runner.RunResult, float]:
    started = time.monotonic()
    result = await job
    return result, round(time.monotonic() - started, 1)


async def upgrade_v8(templates: Path, work: Path) -> dict:
    app_runner = runner.create_runner()
    repo = make_repo(templates / "v8/src", work / "v8")
    result, seconds = await timed(app_runner.upgrade(repo, lambda: None))
    report = json.loads(result.stdout) if result.stdout.startswith("{") else {}
    apply_error = _apply_changes(repo, result.patch) if result.patch else "no patch"
    # The upgraded app has the changes in the index, not in a commit, like a session repo after the tool.
    build, build_seconds = await timed(app_runner.build(repo, lambda: None))
    return {
        "seconds": seconds,
        "studioctl_exit_code": report.get("exitCode"),
        "patch_bytes": len(result.patch),
        "patch_applies": apply_error is None,
        "build_of_the_uncommitted_upgrade_exit_code": build.exit_code,
        "build_seconds": build_seconds,
        "passed": report.get("exitCode") in (0, 3) and apply_error is None and build.exit_code == 0,
    }


async def build_v9(templates: Path, work: Path) -> dict:
    repo = make_repo(templates / "v9/src", work / "v9")
    result, seconds = await timed(runner.create_runner().build(repo, lambda: None))
    return {"seconds": seconds, "exit_code": result.exit_code, "passed": result.exit_code == 0}


async def build_with_a_broken_edit(templates: Path, work: Path) -> dict:
    repo = make_repo(templates / "v9/src", work / "broken")
    Path(repo, "App/logic").mkdir(parents=True, exist_ok=True)
    Path(repo, "App/logic/Broken.cs").write_text("namespace Altinn.App.Logic; public class Broken { int x }\n")
    result, seconds = await timed(runner.create_runner().build(repo, lambda: None))
    found = "Broken.cs" in result.stdout and "error CS" in result.stdout
    return {"seconds": seconds, "exit_code": result.exit_code, "passed": result.exit_code not in (0, None) and found}


async def build_that_calls_home(templates: Path, work: Path) -> dict:
    repo = make_repo(templates / "v9/src", work / "hostile")
    project = Path(repo, "App/App.csproj")
    project.write_text(project.read_text().replace("</Project>", f"{_HOSTILE_TARGET}</Project>"))
    result, seconds = await timed(runner.create_runner().build(repo, lambda: None))
    return {
        "seconds": seconds,
        "exit_code": result.exit_code,
        "curl_failed": "curl" in result.stdout + result.stderr,
        "passed": result.exit_code not in (0, None),
    }


CASES = {
    "upgrade_v8": upgrade_v8,
    "build_v9": build_v9,
    "build_v9_again": build_v9,
    "build_with_a_broken_edit": build_with_a_broken_edit,
    "build_that_calls_home": build_that_calls_home,
}


async def main(templates: Path, results_file: Path, names: list[str]) -> int:
    work = results_file.parent / "apps"
    results = json.loads(results_file.read_text()) if results_file.exists() else {}
    for name in names:
        shutil.rmtree(work / name.removesuffix("_again"), ignore_errors=True)
        try:
            results[name] = await CASES[name](templates, work)
        except Exception as error:  # noqa: BLE001 - the run records each failure and continues
            results[name] = {"passed": False, "error": f"{type(error).__name__}: {error}"}
        print(name, json.dumps(results[name]), flush=True)
        shutil.rmtree(work, ignore_errors=True)
    results_file.write_text(json.dumps(results, indent=2))
    return 0 if all(results[name]["passed"] for name in names) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main(Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3:])))
