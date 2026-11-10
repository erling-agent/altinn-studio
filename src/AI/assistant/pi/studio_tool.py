"""Run one of the assistant's Altinn tools on an app directory, for the pi extension.

The pi extension calls this script instead of porting the Altinn domain library to TypeScript. It
builds the same `LoopContext` the assistant's loop builds, so the tool behaves as it does in the
service. The changed files come from git, not from tool bookkeeping, because pi edits the files
with its own tools.

Usage:
    python pi/studio_tool.py <tool> --repo DIR [--base COMMIT] [--args JSON]

Prints one JSON object: {"is_error": bool, "content": str}.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

ASSISTANT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ASSISTANT_ROOT))
os.environ.setdefault("LANGFUSE_ENABLED", "false")

from agents.altinn.app_version.detection import detect_app_version_profile  # noqa: E402
from agents.core.tool import LoopContext  # noqa: E402
from agents.core.tools import (  # noqa: E402
    DatamodelSyncTool,
    LayoutPropsTool,
    PolicySubjectsTool,
    VerifyChangesTool,
)

TOOLS = {
    "verify_changes": VerifyChangesTool,
    "altinn_layout_props": LayoutPropsTool,
    "altinn_datamodel_sync": DatamodelSyncTool,
    "altinn_policy_subjects": PolicySubjectsTool,
}


def _git(repo: Path, *args: str) -> list[str]:
    out = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout
    return [line for line in out.splitlines() if line.strip()]


def changed_files(repo: Path, base: str) -> set[str]:
    """Files that differ from the start commit, without deleted files."""
    tracked = _git(repo, "diff", "--name-only", "--diff-filter=d", base)
    untracked = _git(repo, "ls-files", "--others", "--exclude-standard")
    return set(tracked) | set(untracked)


async def run(tool_name: str, repo: Path, base: str, raw_args: dict) -> dict:
    tool = TOOLS[tool_name]()
    ctx = LoopContext(
        session_id="pi",
        repo_path=str(repo),
        allow_app_changes=True,
        app_version_profile=detect_app_version_profile(str(repo)),
        repo_owner=os.environ.get("STUDIO_REPO_OWNER", ""),
        designer_api_key=os.environ.get("STUDIO_DESIGNER_API_KEY") or None,
        extras={
            "changed_files": changed_files(repo, base),
            "app_name": os.environ.get("STUDIO_APP_NAME", repo.name),
        },
    )
    args = tool.input_schema.model_validate(raw_args)
    result = await tool.run(args, ctx)
    return {"is_error": result.is_error, "content": result.content}


def describe() -> dict:
    """The parameter schema of each tool, so the extension does not repeat them."""
    return {name: tool.input_schema.model_json_schema() for name, tool in TOOLS.items()}


def main() -> None:
    if sys.argv[1:] == ["describe"]:
        print(json.dumps(describe()))
        return
    parser = argparse.ArgumentParser()
    parser.add_argument("tool", choices=sorted(TOOLS))
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--base", default="HEAD")
    parser.add_argument("--args", default="{}")
    ns = parser.parse_args()
    try:
        result = asyncio.run(run(ns.tool, ns.repo.resolve(), ns.base, json.loads(ns.args)))
    except Exception as exc:
        result = {"is_error": True, "content": f"{ns.tool} failed: {type(exc).__name__}: {exc}"}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
