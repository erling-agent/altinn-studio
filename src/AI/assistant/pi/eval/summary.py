"""Summarize eval runs per task and per harness, as Markdown tables.

    python pi/eval/summary.py pi/runs/wide
"""

from __future__ import annotations

import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path


def load(root: Path) -> list[dict]:
    return [json.loads(f.read_text()) for f in sorted(root.glob("*/*/result.json"))]


def fmt(values: list[float], digits: int = 2) -> str:
    return f"{statistics.mean(values):.{digits}f}" if values else "–"


def main() -> None:
    runs = load(Path(sys.argv[1]))
    by_task: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for run in runs:
        by_task[run["task"]][run["harness"]].append(run)

    print("| Task | Assistant score | pi score | Assistant full marks | pi full marks |")
    print("|---|---|---|---|---|")
    for task, harnesses in by_task.items():
        a, p = harnesses.get("assistant", []), harnesses.get("pi", [])
        print(
            f"| `{task}` | {fmt([r['score'] for r in a])} | {fmt([r['score'] for r in p])} | "
            f"{sum(r['score'] == 1.0 for r in a)}/{len(a)} | {sum(r['score'] == 1.0 for r in p)}/{len(p)} |"
        )

    print()
    print("| Harness | Runs | Mean score | Full marks | Mean cost $ | Mean seconds | Mean model calls |")
    print("|---|---|---|---|---|---|---|")
    for harness in ("assistant", "pi"):
        rs = [r for r in runs if r["harness"] == harness]
        print(
            f"| {harness} | {len(rs)} | {fmt([r['score'] for r in rs])} | {sum(r['score'] == 1.0 for r in rs)} | "
            f"{fmt([r.get('cost_usd', 0) for r in rs], 3)} | {fmt([r.get('seconds') or 0 for r in rs], 0)} | "
            f"{fmt([r.get('model_calls') or 0 for r in rs], 1)} |"
        )

    print()
    print("Runs below full marks:")
    for run in sorted(runs, key=lambda r: (r["task"], r["harness"], r["rep"])):
        if run["score"] < 1.0:
            fails = [f"{k}: {v['comment'][:140]}" for k, v in run.get("scores", {}).items() if v["value"] < 1]
            print(f"- {run['harness']} {run['task']} rep {run['rep']} ({run['score']}): {'; '.join(fails)}")


if __name__ == "__main__":
    main()
