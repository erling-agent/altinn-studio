"""Task-specific checks on a built app, beside the structural rubric of the e2e benchmark.

Each check reads only the app's files and returns (passed, comment). The rubric's scores come from
`benchmarks.evaluators`, so a run here scores the same way as `Benchmarks/forms`.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from benchmarks.app_model import AppModel, titles_match

Check = tuple[bool, str]
STUDIO_TOOL = Path(__file__).resolve().parents[1] / "studio_tool.py"

BPMN = "{http://www.omg.org/spec/BPMN/20100524/MODEL}"
ALTINN = "{http://altinn.no/process}"


def _components_titled(app: AppModel, title: str) -> list[dict]:
    return [c for c in app.ordered_components() if (t := app.title_of(c)) and titles_match(title, t)]


def _schema_paths(schema: dict, prefix: str = "") -> set[str]:
    paths: set[str] = set()
    defs = schema.get("$defs") or schema.get("definitions") or {}

    def walk(node: dict, path: str, depth: int) -> None:
        if depth > 8 or not isinstance(node, dict):
            return
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.split("/")[-1] in defs:
            walk(defs[ref.split("/")[-1]], path, depth + 1)
        for name, child in (node.get("properties") or {}).items():
            child_path = f"{path}.{name}" if path else name
            paths.add(child_path)
            walk(child, child_path, depth + 1)
        if isinstance(node.get("items"), dict):
            walk(node["items"], path, depth + 1)

    walk(schema, prefix, 0)
    return paths


def bindings_in_model(root: Path, app: AppModel, task: dict[str, Any]) -> Check:
    """Every simpleBinding names a property in a data model schema."""
    paths: set[str] = set()
    for schema_file in (root / "App" / "models").glob("*.schema.json"):
        try:
            paths |= _schema_paths(json.loads(schema_file.read_text(encoding="utf-8-sig")))
        except OSError, json.JSONDecodeError:
            continue
    bindings = []
    for component in app.ordered_components():
        for value in (component.get("dataModelBindings") or {}).values():
            field = value.get("field") if isinstance(value, dict) else value
            if isinstance(field, str):
                bindings.append(field)
    if not bindings:
        return False, "no data model bindings"
    missing = sorted({b for b in bindings if b not in paths})
    return not missing, f"{len(bindings) - len(missing)}/{len(bindings)} bindings resolve" + (
        f"; missing: {missing[:6]}" if missing else ""
    )


def required_fields(root: Path, app: AppModel, task: dict[str, Any]) -> Check:
    wrong = []
    for title in task["required"]:
        components = _components_titled(app, title)
        if not components or components[0].get("required") is not True:
            wrong.append(f"{title} not required")
    for title in task["optional"]:
        components = _components_titled(app, title)
        if components and components[0].get("required") is True:
            wrong.append(f"{title} required")
    return not wrong, "; ".join(wrong) or "required and optional fields as asked"


def datepicker(root: Path, app: AppModel, task: dict[str, Any]) -> Check:
    components = _components_titled(app, "Flyttedato")
    if not components:
        return False, "no Flyttedato field"
    kind = components[0].get("type")
    return kind == "Datepicker", f"Flyttedato is {kind}"


def conditional_field(root: Path, app: AppModel, task: dict[str, Any]) -> Check:
    questions = _components_titled(app, task["question"])
    dependents = _components_titled(app, task["dependent"])
    if not questions or not dependents:
        return False, "question or dependent field missing"
    question, dependent = questions[0], dependents[0]
    if question.get("type") != "RadioButtons":
        return False, f"question is {question.get('type')}, not RadioButtons"
    binding = (question.get("dataModelBindings") or {}).get("simpleBinding")
    field = binding.get("field") if isinstance(binding, dict) else binding
    hidden = dependent.get("hidden")
    if not isinstance(hidden, list):
        return False, f"dependent field's hidden is {json.dumps(hidden)}, not an expression"
    if not field or field not in json.dumps(hidden):
        return False, f"hidden expression does not read {field}: {json.dumps(hidden)}"
    return True, f"hidden = {json.dumps(hidden, ensure_ascii=False)}"


def code_list(root: Path, app: AppModel, task: dict[str, Any]) -> Check:
    components = _components_titled(app, task["list_title"])
    if not components:
        return False, "no Sivilstand component"
    component = components[0]
    if component.get("type") != "Dropdown":
        return False, f"component is {component.get('type')}, not Dropdown"
    options_id = component.get("optionsId")
    if not options_id:
        return False, "Dropdown has no optionsId"
    option_file = root / "App" / "options" / f"{options_id}.json"
    if not option_file.exists():
        return False, f"App/options/{options_id}.json does not exist"
    try:
        options = json.loads(option_file.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        return False, f"options file is not JSON: {exc}"
    labels = [app.resources.get(str(o.get("label")), str(o.get("label"))) for o in options if isinstance(o, dict)]
    missing = [o for o in task["options"] if not any(titles_match(o, label) for label in labels)]
    return not missing, f"{len(labels)} options" + (f"; missing {missing}" if missing else "")


def confirmation_task(root: Path, app: AppModel, task: dict[str, Any]) -> Check:
    try:
        tree = ET.parse(root / "App" / "config" / "process" / "process.bpmn")
    except ET.ParseError as exc:
        return False, f"process.bpmn does not parse: {exc}"
    process = tree.getroot().find(f"{BPMN}process")
    if process is None:
        return False, "no process element"
    flows = {f.get("id"): (f.get("sourceRef"), f.get("targetRef")) for f in process.iter(f"{BPMN}sequenceFlow")}
    confirm_tasks = [
        t.get("id")
        for t in process
        if (kind := t.find(f".//{ALTINN}taskType")) is not None and (kind.text or "").strip() == "confirmation"
    ]
    if not confirm_tasks:
        return False, "no task with taskType confirmation"
    confirm_id = confirm_tasks[0]

    def reachable(start: str) -> set[str]:
        seen, todo = {start}, [start]
        while todo:
            node = todo.pop()
            for source, target in flows.values():
                if source == node and target not in seen:
                    seen.add(target)
                    todo.append(target)
        return seen

    end_ids = [e.get("id") for e in process.iter(f"{BPMN}endEvent")]
    if confirm_id not in reachable("Task_1") or not any(e in reachable(confirm_id) for e in end_ids):
        return False, f"{confirm_id} is not on the path from Task_1 to the end"
    task_element = next(t for t in process if t.get("id") == confirm_id)
    actions = [a.text for a in task_element.iter(f"{ALTINN}action")]
    if "confirm" not in actions:
        return False, f"{confirm_id} has no confirm action (actions: {actions})"
    policy = (root / "App" / "config" / "authorization" / "policy.xml").read_text(encoding="utf-8-sig")
    if not re.search(r">\s*confirm\s*<", policy):
        return False, "policy.xml gives no confirm right"
    return True, f"{confirm_id} confirms between Task_1 and the end, and the policy permits confirm"


def _git(root: Path, *args: str) -> list[str]:
    out = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True).stdout
    return [line for line in out.splitlines() if line.strip()]


def models_generated(root: Path, app: AppModel, task: dict[str, Any]) -> Check:
    """The C# class and the XSD are what Designer generates from the schema, not a hand edit."""
    changed = set(_git(root, "diff", "--name-only", task["_base"], "--", "App/models")) | set(
        _git(root, "ls-files", "--others", "--exclude-standard", "--", "App/models")
    )
    schemas = sorted(f for f in changed if f.endswith(".schema.json"))
    if not schemas:
        return False, "no data model schema changed"
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "app"
        shutil.copytree(root, copy)
        for schema in schemas:
            out = subprocess.run(
                [sys.executable, str(STUDIO_TOOL), "altinn_datamodel_sync", "--repo", str(copy),
                 "--args", json.dumps({"schema_path": schema})],
                capture_output=True, text=True,
            ).stdout  # fmt: skip
            if json.loads(out)["is_error"]:
                return False, f"Designer could not generate {schema}: {json.loads(out)['content'][:200]}"
        differ = []
        for generated in sorted((copy / "App" / "models").glob("*")):
            if generated.suffix not in (".cs", ".xsd"):
                continue
            ours = root / "App" / "models" / generated.name
            normalize = lambda text: " ".join(text.split())  # noqa: E731
            if not ours.exists() or normalize(ours.read_text(encoding="utf-8-sig")) != normalize(
                generated.read_text(encoding="utf-8-sig")
            ):
                differ.append(generated.name)
    return not differ, f"differ from Designer's output: {differ}" if differ else "C# and XSD match Designer's output"


def chat_answer(root: Path, app: AppModel, task: dict[str, Any]) -> Check:
    if task["_files"]:
        return False, f"a read-only turn changed files: {task['_files']}"
    answer = task["_final_text"]
    named = [t for t in task["required"] if t.lower() in answer.lower()]
    mentions_required = "required" in answer or "påkrevd" in answer.lower()
    ok = len(named) >= len(task["required"]) - 1 and "Telefonnummer" in answer and mentions_required
    return ok, f"names {len(named)}/{len(task['required'])} required fields; mentions required: {mentions_required}"


def followup_kommune(root: Path, app: AppModel, task: dict[str, Any]) -> Check:
    for page in app.page_order:
        components = app.layouts.get(page, [])
        titles = [app.title_of(c) or "" for c in components]
        kommune = next((i for i, t in enumerate(titles) if titles_match("Kommune", t)), None)
        poststed = next((i for i, t in enumerate(titles) if titles_match("Poststed", t)), None)
        if kommune is None:
            continue
        if poststed is None or kommune != poststed + 1:
            return False, f"Kommune is at {kommune} on {page}, Poststed at {poststed}"
        if components[kommune].get("required") is True:
            return False, "Kommune is still required after the follow-up"
        return True, f"Kommune follows Poststed on {page} and is optional"
    return False, "no Kommune field"


def english_texts(root: Path, app: AppModel, task: dict[str, Any]) -> Check:
    path = root / "App" / "config" / "texts" / "resource.en.json"
    if not path.exists():
        return False, "no resource.en.json"
    try:
        english = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        return False, f"resource.en.json is not JSON: {exc}"
    values = {r.get("id"): str(r.get("value", "")) for r in english.get("resources") or [] if isinstance(r, dict)}
    missing = sorted(set(app.resources) - set(values))
    same = [k for k, v in app.resources.items() if values.get(k, "").strip().lower() == v.strip().lower()]
    ok = english.get("language") == "en" and not missing and len(same) <= len(app.resources) // 5
    return (
        ok,
        f"language {english.get('language')}; missing {missing[:5]}; {len(same)}/{len(app.resources)} untranslated",
    )


CHECKS = {
    "models_generated": models_generated,
    "chat_answer": chat_answer,
    "followup_kommune": followup_kommune,
    "english_texts": english_texts,
    "bindings_in_model": bindings_in_model,
    "required_fields": required_fields,
    "datepicker": datepicker,
    "conditional_field": conditional_field,
    "code_list": code_list,
    "confirmation_task": confirmation_task,
}
