"""Two-way conversion between DAGonWeb workflows and DAGonStar Python programs.

Export
    :func:`workflow_python_source` writes a self-contained, commented,
    headless DAGonStar program; :func:`python_bundle` packs it in a ZIP with
    the JSON document, CWL, ``dagon.ini``, ``requirements.txt`` and a README.

Import
    :func:`import_python_files` reads a DAGonStar application (one ``.py``
    file or a ZIP) with :mod:`ast` only: **nothing is executed**. It finds
    ``Workflow(...)``, ``DagonTask(TaskType.X, ...)``, ``add_task`` and
    ``add_dependency_to`` calls, resolves literal arguments and simple
    module-level constants, and returns a DAGonWeb document. Docstrings and
    the comments above each task are kept in the ``dagonweb.python``
    extension, so exporting an imported program gives back a working and
    commented program.
"""
from __future__ import annotations

import ast
import io
import io as _io
import json
import pprint
import re
import tokenize
import zipfile
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from .dagon_ini import dump_dagon_ini, parse_dagon_ini
from .interactive import INTERACTIVE_TYPES
from .models import LINK_DATA, LINK_EXPLICIT, Workflow
from .task_specs import TASK_SPECS, WORKFLOW_REFERENCE, config_from_arguments, dagon_task_arguments, runtime_type

SECRET_KEY_HINTS = ("key", "secret", "token", "password", "credential", "authorization")
MAX_ARCHIVE_FILES = 500
MAX_ARCHIVE_BYTES = 20 * 1024 * 1024
MAX_SOURCE_BYTES = 2 * 1024 * 1024
DAGONSTAR_REQUIREMENT = "dagonstar @ git+https://github.com/DagOnStar/dagonstar.git@a7e633feab5c28ec8624a16f1c95e5499ddc93d4"
MARKER = "# dagonweb:"


# ===========================================================================
# Export
# ===========================================================================


def _literal(value: Any, indent: int) -> str:
    """Render a Python literal readably, continuing lines at ``indent``."""
    pad = " " * indent
    if isinstance(value, str) and "\n" in value:
        lines = value.splitlines(keepends=True)
        if len(lines) > 1:
            return "(\n" + "".join(f"{pad}    {line!r}\n" for line in lines) + f"{pad})"
    text = pprint.pformat(value, width=max(60, 100 - indent), sort_dicts=False)
    return text.replace("\n", "\n" + pad)


def _identifiers(workflow: Workflow) -> dict[str, str]:
    identifiers: dict[str, str] = {}
    used: set[str] = set()
    for task in workflow.tasks:
        base = re.sub(r"\W|^(?=\d)", "_", task.name)
        identifier = f"task_{base}"
        suffix = 2
        while identifier in used:
            identifier = f"task_{base}_{suffix}"
            suffix += 1
        identifiers[task.name] = identifier
        used.add(identifier)
    return identifiers


def portable_ini_sections(workflow: Workflow) -> dict[str, dict[str, str]]:
    """Agent profile sections safe to embed in a generated program."""
    sections: dict[str, dict[str, str]] = {}
    for section, values in workflow.dagon_ini.items():
        if not isinstance(values, dict) or not (section.startswith("agent.") or section.startswith("agent_provider.")):
            continue
        sections[section] = {str(key): str(value) for key, value in values.items() if not _is_secret(key)}
    return sections


def _is_secret(key: Any) -> bool:
    return any(hint in str(key).lower() for hint in SECRET_KEY_HINTS)


def sanitized_dagon_ini(workflow: Workflow) -> dict[str, dict[str, str]]:
    """The workflow's dagon.ini sections with secret-looking values blanked."""
    return {section: {str(key): ("" if _is_secret(key) else str(value)) for key, value in values.items()} for section, values in workflow.dagon_ini.items() if isinstance(values, dict)}


def _task_comment(workflow: Workflow, task: Any, python_meta: dict[str, Any]) -> list[str]:
    """Explain a task: what it is, what it reads, what it waits for."""
    spec = TASK_SPECS.get(task.normalized_task_type)
    lines: list[str] = []
    preserved = (python_meta.get("comments") or {}).get(task.name)
    if isinstance(preserved, str) and preserved.strip():
        lines.extend(preserved.rstrip().splitlines())
    else:
        label = spec.label if spec else task.normalized_task_type
        description = spec.description if spec else ""
        lines.append(f"{label} task. {description}".strip())
        if spec and spec.interactive:
            lines.append("DAGonWeb interactive task, executed by DAGonStar as a Batch task.")
    inputs = sorted({f"workflow:///{link.source_uid}/{link.source_output}" for link in workflow.links if link.target_uid == task.name and link.kind == LINK_DATA})
    if inputs:
        lines.append("Reads: " + ", ".join(inputs))
    after = sorted(link.source_uid for link in workflow.links if link.target_uid == task.name and link.kind == LINK_EXPLICIT)
    if after:
        lines.append("Runs after: " + ", ".join(after))
    return lines


def _lossy(task_type: str, name: str, config: dict[str, Any], args: list[Any], kwargs: dict[str, Any]) -> bool:
    try:
        return config_from_arguments(task_type, args, kwargs) != (name, config)
    except Exception:
        return True


def workflow_python_source(workflow: Workflow) -> str:
    """Generate a commented, headless DAGonStar program for ``workflow``."""
    identifiers = _identifiers(workflow)
    metadata = workflow.dagonweb
    python_meta = metadata.get("python") if isinstance(metadata.get("python"), dict) else {}
    native_sources: dict[str, str] = {}
    interactive: dict[str, dict[str, Any]] = {}
    blocks: list[str] = []
    for task in workflow.tasks:
        task_type = task.normalized_task_type
        args, kwargs = dagon_task_arguments(task_type, task.name, task.config)
        rendered = [f"TaskType.{runtime_type(task_type).upper()}"]
        rendered += [_literal(arg, 8) for arg in args]
        rendered += [f"{key}={_literal(value, 8)}" for key, value in kwargs.items()]
        call = ",\n        ".join(rendered)
        comment = "\n".join(f"    # {line}" if line else "    #" for line in _task_comment(workflow, task, python_meta))
        layout: dict[str, Any] = {"x": task.x, "y": task.y}
        if task_type not in INTERACTIVE_TYPES and _lossy(task_type, task.name, task.config, args, kwargs):
            # The DagonTask arguments do not carry every editor detail (for
            # example checkpoint input names): keep the configuration too.
            layout["config"] = task.config
        marker = f"    {MARKER} {json.dumps(layout, sort_keys=True)}"
        blocks.append(
            f"    # --- {task.name} ({TASK_SPECS[task_type].label if task_type in TASK_SPECS else task_type}) "
            + "-" * max(4, 60 - len(task.name))
            + f"\n{comment}\n{marker}\n    {identifiers[task.name]} = DagonTask(\n        {call},\n    )\n    workflow.add_task({identifiers[task.name]})\n"
        )
        if task_type == "native":
            source = task.config.get("source")
            module = str(task.config.get("callable", "")).partition(":")[0]
            if isinstance(source, str) and source.strip() and module:
                native_sources[module] = source.rstrip() + "\n"
        if task_type in INTERACTIVE_TYPES:
            interactive[task.name] = {"kind": task_type, "config": task.config}

    explicit = [
        f"    {identifiers[link.target_uid]}.add_dependency_to({identifiers[link.source_uid]})"
        for link in workflow.links
        if link.kind == LINK_EXPLICIT and link.source_uid in identifiers and link.target_uid in identifiers
    ]
    options = workflow.runtime_options
    workflow_kwargs = ["config=local_config(workdir)", 'checkpoint_file=str(workdir / "checkpoint.json")']
    if options.get("max_threads"):
        workflow_kwargs.append(f"max_threads={int(options['max_threads'])}")
    if options.get("portable_emulation"):
        workflow_kwargs.append("portable_emulation=True")
    if options.get("pre_stage"):
        workflow_kwargs.append("pre_stage=True")
    keep_metadata = {key: value for key, value in metadata.items() if key not in ("python",)}
    docstring = str(python_meta.get("docstring") or workflow.description or "").strip()
    description = "\n".join(line.rstrip() for line in docstring.replace('"""', "'''").replace("\\", "\\\\").splitlines())
    header_lines = [f"{workflow.name}: a DAGonStar workflow."]
    if description:
        header_lines += ["", description]

    native_block = ""
    if native_sources:
        native_block = f'''

# Source code of the Native tasks. It is written below the working directory
# and put on sys.path so DAGonStar can import ``module:function`` callables.
NATIVE_SOURCES = {_literal(native_sources, 0)}


def install_native_sources(workdir: Path) -> None:
    """Write the embedded Native task modules and make them importable."""
    module_root = workdir / "native_modules"
    for module, source in NATIVE_SOURCES.items():
        parts = module.split(".")
        target = module_root.joinpath(*parts[:-1], parts[-1] + ".py")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source, encoding="utf-8")
        for depth in range(1, len(parts)):
            module_root.joinpath(*parts[:depth], "__init__.py").touch()
    sys.path.insert(0, str(module_root))
'''
    interactive_block = ""
    if interactive:
        interactive_block = f'''

# DAGonWeb interactive tasks. In DAGonWeb they wait for the user on the run
# page; headless, their data comes from the command line (or a prompt).
INTERACTIVE_TASKS = {_literal(interactive, 0)}


def prepare_interactive_inputs(data_dir: Path, uploads: dict[str, list[str]], forms: dict[str, str]) -> None:
    """Fill DAGONWEB_DATA/<task> for Upload and Form tasks, then mark it ready."""
    for name, task in INTERACTIVE_TASKS.items():
        target = data_dir / name
        if task["kind"] == "upload":
            files = target / "files"
            files.mkdir(parents=True, exist_ok=True)
            sources = uploads.get(name, [])
            if not sources and not any(files.iterdir()):
                raise SystemExit(f"Upload task {{name}} needs data: --input {{name}}=PATH")
            save_as = task["config"].get("save_as") or "data.csv"
            for source in sources:
                destination = files / (Path(source).name if task["config"].get("multiple") else save_as)
                shutil.copyfile(source, destination)
            (target / ".ready").touch()
        elif task["kind"] == "form":
            target.mkdir(parents=True, exist_ok=True)
            fields = task["config"].get("fields") or []
            if name in forms:
                values = json.loads(Path(forms[name]).read_text(encoding="utf-8"))
            else:
                values = {{}}
                for item in fields:
                    default = item.get("default", "")
                    if sys.stdin.isatty():
                        answer = input(f"{{item.get('label') or item['name']}} [{{default}}]: ").strip()
                        values[item["name"]] = answer or default
                    else:
                        values[item["name"]] = default
            (target / "form.json").write_text(json.dumps(values, indent=2, sort_keys=True) + "\\n", encoding="utf-8")
            env = "".join(f"export {{key.upper()}}={{shlex.quote(str(value))}}\\n" for key, value in values.items())
            (target / "form.env").write_text(env, encoding="utf-8")
            (target / ".ready").touch()
'''
    usage_inputs = ""
    if interactive:
        usage_inputs = (
            "\n    python {file} --input TASK=FILE     # data for an Upload task (repeatable)"
            "\n    python {file} --form TASK=FILE.json # values for a Form task (otherwise prompted)"
        ).format(file=f"{workflow.name}.py")
    return f'''#!/usr/bin/env python3
"""{chr(10).join(header_lines)}

Generated by DAGonWeb for DAGonStar 2.9. The program is headless and needs only
DAGonStar (``pip install -r requirements.txt``).

Usage:
    python {workflow.name}.py                    # run in ./scratch
    python {workflow.name}.py --resume           # continue a failed run from its checkpoint
    python {workflow.name}.py --workdir DIR      # use another scratch directory{usage_inputs}

Data dependencies are the ``workflow:///<task>/<path>`` references inside task
commands and configurations; DAGonStar stages the files and orders the tasks.
Import this file (or its ZIP bundle) back into DAGonWeb to edit it visually:
the ``{MARKER}`` comments keep the canvas layout.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import sys
from pathlib import Path

from dagon import Workflow
from dagon.config import read_config
from dagon.task import DagonTask, TaskType

HERE = Path(__file__).resolve().parent
WORKFLOW_NAME = {workflow.name!r}

# DAGonWeb metadata (FAIR profile, runtime options). It is not used by
# DAGonStar; it lets DAGonWeb restore the workflow exactly on import.
DAGONWEB_METADATA = {_literal(keep_metadata, 0)}

# Agent and provider profiles from the workflow's dagon.ini (secrets removed).
EXTRA_SECTIONS = {_literal(portable_ini_sections(workflow), 0)}{native_block}{interactive_block}


def local_config(workdir: Path) -> dict:
    """Read ./dagon.ini (or the one next to this file) and root scratch at ``workdir``."""
    candidates = [Path("dagon.ini"), HERE / "dagon.ini"]
    path = next((candidate for candidate in candidates if candidate.is_file()), None)
    config = read_config(str(path)) if path else {{}}
    for section, values in EXTRA_SECTIONS.items():
        config.setdefault(section, dict(values))
    config.setdefault("batch", {{}}).update({{"scratch_dir_base": str(workdir), "remove_dir": False}})
    config.setdefault("ftp_pub", {{}})
    config.setdefault("dagon_service", {{"use": "False"}})
    return config


def build_workflow(workdir: Path) -> Workflow:
    """Create the DAGonStar workflow and declare its tasks."""
    workdir.mkdir(parents=True, exist_ok=True)
{"    install_native_sources(workdir)" + chr(10) if native_sources else ""}    workflow = Workflow(
        WORKFLOW_NAME,
        {(","+chr(10)+"        ").join(workflow_kwargs)},
    )

{chr(10).join(blocks)}
{("    # Explicit dependencies (ordering without data staging, DAGonStar explicit_prevs)." + chr(10) + chr(10).join(explicit) + chr(10)) if explicit else ""}
    # Discover data dependencies from the workflow:/// references.
    workflow.make_dependencies()
    return workflow


def parse_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=f"Run the {{WORKFLOW_NAME}} DAGonStar workflow.")
    parser.add_argument("--workdir", type=Path, default=Path.cwd() / "scratch", help="scratch directory (default: ./scratch)")
    parser.add_argument("--resume", action="store_true", help="resume a failed run from its checkpoint")
    parser.add_argument("--input", action="append", default=[], metavar="TASK=FILE", help="file for an Upload task")
    parser.add_argument("--form", action="append", default=[], metavar="TASK=FILE", help="JSON values for a Form task")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    arguments = parse_arguments(argv)
    workdir = arguments.workdir.resolve()
    # Interactive (DAGonWeb) task scripts read these two variables.
    data_dir = workdir / "dagonweb_data"
    os.environ.setdefault("DAGONWEB_DATA", str(data_dir))
    os.environ.setdefault("DAGONWEB_PYTHON", sys.executable)
    uploads: dict[str, list[str]] = {{}}
    for item in arguments.input:
        name, _, path = item.partition("=")
        uploads.setdefault(name, []).append(path)
    forms = dict(item.partition("=")[::2] for item in arguments.form)
{"    prepare_interactive_inputs(data_dir, uploads, forms)" + chr(10) if interactive else "    if uploads or forms:" + chr(10) + "        print(\"This workflow has no interactive tasks; --input/--form ignored.\")" + chr(10)}
    workflow = build_workflow(workdir)
    checkpoint = workdir / "checkpoint.json"
    resume = str(checkpoint) if arguments.resume and checkpoint.is_file() else None
    workflow.launch(resume_checkpoint_file=resume)
    workflow.wait()
    failed = [task.name for task in workflow.tasks if task.status.name != "FINISHED"]
    print("Failed tasks: " + ", ".join(failed) if failed else "Workflow completed.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
'''


def readme(workflow: Workflow, has_cwl: bool) -> str:
    interactive = [task for task in workflow.tasks if task.normalized_task_type in ("upload", "form")]
    lines = [
        f"# {workflow.name}",
        "",
        (workflow.description or "A DAGonStar workflow exported from DAGonWeb.").strip(),
        "",
        "## Contents",
        "",
        f"- `{workflow.name}.py` — headless DAGonStar program (commented, self-contained).",
        f"- `{workflow.name}.json` — DAGonStar task-map document (`dagon.Workflow.load_json`), with the DAGonWeb extension.",
    ]
    if has_cwl:
        lines.append(f"- `{workflow.name}.cwl` — Common Workflow Language v1.2 rendering produced by DAGonStar.")
    lines += [
        "- `dagon.ini` — DAGonStar configuration (secrets removed; fill them in locally).",
        "- `requirements.txt` — Python requirements.",
        "- `run.sh` — creates a virtual environment and runs the workflow.",
        "",
        "## Run",
        "",
        "```bash",
        "./run.sh            # or: pip install -r requirements.txt && python " + f"{workflow.name}.py",
        f"python {workflow.name}.py --resume   # continue a failed run",
        "```",
        "",
        "Task directories, the checkpoint and outputs are written below `./scratch`.",
    ]
    if interactive:
        lines += ["", "## Interactive inputs", ""]
        for task in interactive:
            if task.normalized_task_type == "upload":
                lines.append(f"- `{task.name}`: `--input {task.name}=path/to/file`")
            else:
                lines.append(f"- `{task.name}`: `--form {task.name}=values.json` (prompted when omitted)")
    lines += ["", "Import this ZIP into DAGonWeb (Workflows → Import) to edit the workflow visually.", ""]
    return "\n".join(lines)


def python_bundle(workflow: Workflow, cwl_source: str | None) -> bytes:
    """ZIP with the program, documents, configuration and instructions."""
    base = PurePosixPath(workflow.name)
    requirements = [DAGONSTAR_REQUIREMENT]
    for task in workflow.tasks:
        extra = task.config.get("requirements") if task.normalized_task_type == "native" else None
        if isinstance(extra, str) and extra.strip():
            requirements += [f"# Native task {task.name}", *[line for line in extra.splitlines() if line.strip()]]
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as bundle:
        def add(name: str, content: str, mode: int = 0o644) -> None:
            info = zipfile.ZipInfo(str(base / name), date_time=(2026, 1, 1, 0, 0, 0))
            info.external_attr = (0o100000 | mode) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            bundle.writestr(info, content)

        add(f"{workflow.name}.py", workflow_python_source(workflow), 0o755)
        document = workflow.as_json()
        document["id"] = None
        add(f"{workflow.name}.json", json.dumps(document, indent=2, sort_keys=True) + "\n")
        if cwl_source:
            add(f"{workflow.name}.cwl", cwl_source)
        add("dagon.ini", dump_dagon_ini(sanitized_dagon_ini(workflow)) if workflow.dagon_ini else "# Add DAGonStar back-end settings here.\n")
        add("requirements.txt", "\n".join(requirements) + "\n")
        add("run.sh", f'#!/usr/bin/env sh\nset -eu\ncd "$(dirname "$0")"\n[ -d .venv ] || python3 -m venv .venv\n. .venv/bin/activate\npip install -q -r requirements.txt\nexec python {workflow.name}.py "$@"\n', 0o755)
        add("README.md", readme(workflow, bool(cwl_source)))
    return buffer.getvalue()


# ===========================================================================
# Import
# ===========================================================================


class PythonImportError(ValueError):
    """The upload is not a DAGonStar application DAGonWeb can read."""


@dataclass
class ParsedTask:
    variable: str | None
    name: str
    task_type: str
    config: dict[str, Any]
    line: int
    comment: str = ""
    layout: dict[str, Any] = field(default_factory=dict)
    arguments: tuple[list[Any], dict[str, Any]] = field(default_factory=lambda: ([], {}))


class _Resolver:
    """Evaluate literal expressions, following simple module constants."""

    def __init__(self, constants: dict[str, Any]) -> None:
        self.constants = constants

    def value(self, node: ast.AST) -> Any:
        if isinstance(node, ast.Name) and node.id in self.constants:
            return self.constants[node.id]
        if isinstance(node, ast.JoinedStr):
            parts = []
            for item in node.values:
                if isinstance(item, ast.Constant):
                    parts.append(str(item.value))
                elif isinstance(item, ast.FormattedValue) and item.format_spec is None and item.conversion == -1:
                    parts.append(str(self.value(item.value)))
                else:
                    raise ValueError("unsupported f-string")
            return "".join(parts)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            return self.value(node.left) + self.value(node.right)
        if isinstance(node, (ast.List, ast.Tuple)):
            return [self.value(item) for item in node.elts]
        if isinstance(node, ast.Dict):
            if any(key is None for key in node.keys):
                raise ValueError("dictionary unpacking is not supported")
            return {self.value(key): self.value(item) for key, item in zip(node.keys, node.values)}
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("str", "dict", "list", "int", "float") and not node.keywords and len(node.args) <= 1:
            return {"str": str, "dict": dict, "list": list, "int": int, "float": float}[node.func.id](*(self.value(arg) for arg in node.args))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "dict" and not node.args:
            return {keyword.arg: self.value(keyword.value) for keyword in node.keywords if keyword.arg}
        return ast.literal_eval(node)


def _comments(source: str) -> dict[int, str]:
    found: dict[int, str] = {}
    try:
        for token in tokenize.generate_tokens(_io.StringIO(source).readline):
            if token.type == tokenize.COMMENT:
                found[token.start[0]] = token.string
    except (tokenize.TokenError, IndentationError):
        pass
    return found


def _leading_comment(lines: list[str], comments: dict[int, str], line: int) -> tuple[str, dict[str, Any]]:
    """Collect the contiguous comment block right above ``line``."""
    collected: list[str] = []
    layout: dict[str, Any] = {}
    current = line - 1
    while current >= 1 and current in comments and lines[current - 1].strip().startswith("#"):
        text = comments[current]
        if text.startswith(MARKER):
            try:
                value = json.loads(text[len(MARKER):].strip())
                if isinstance(value, dict):
                    layout = value
            except json.JSONDecodeError:
                pass
        elif not re.match(r"#\s*---", text):
            collected.append(re.sub(r"^#\s?", "", text))
        current -= 1
    collected.reverse()
    generated = re.compile(r"^(Reads|Runs after): |^DAGonWeb interactive task, executed")
    kept = [line for line in collected if not generated.match(line)]
    if len(kept) == 1 and any(kept[0].startswith(f"{spec.label} task.") for spec in TASK_SPECS.values()):
        kept = []
    return "\n".join(kept).strip(), layout


def _task_type(node: ast.AST) -> str:
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "TaskType":
        return node.attr.lower()
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value.lower()
    raise PythonImportError("DagonTask's first argument must be TaskType.<TYPE>.")


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _parse_program(source: str, filename: str) -> dict[str, Any]:
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError as exc:
        raise PythonImportError(f"{filename} line {exc.lineno}: {exc.msg}") from exc
    lines = source.splitlines()
    comments = _comments(source)
    constants: dict[str, Any] = {}
    for statement in tree.body:
        if isinstance(statement, (ast.Assign, ast.AnnAssign)):
            targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
            if len(targets) == 1 and isinstance(targets[0], ast.Name) and statement.value is not None:
                try:
                    constants[targets[0].id] = _Resolver(constants).value(statement.value)
                except Exception:
                    pass
    resolver = _Resolver(constants)
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent

    workflow_name = None
    workflow_options: dict[str, Any] = {}
    tasks: list[ParsedTask] = []
    by_variable: dict[str, ParsedTask] = {}
    explicit: list[tuple[str, str]] = []
    warnings: list[str] = []
    calls = sorted((node for node in ast.walk(tree) if isinstance(node, ast.Call)), key=lambda node: (node.lineno, node.col_offset))
    for call in calls:
        name = _call_name(call.func)
        if name == "Workflow" and workflow_name is None and call.args:
            try:
                workflow_name = str(resolver.value(call.args[0]))
            except Exception:
                workflow_name = None
            for keyword in call.keywords:
                if keyword.arg in ("portable_emulation", "pre_stage", "max_threads"):
                    try:
                        workflow_options[keyword.arg] = resolver.value(keyword.value)
                    except Exception:
                        pass
        elif name == "DagonTask":
            if not call.args:
                raise PythonImportError(f"{filename} line {call.lineno}: DagonTask needs a TaskType.")
            dagon_type = _task_type(call.args[0])
            if dagon_type not in TASK_SPECS or TASK_SPECS[dagon_type].interactive:
                raise PythonImportError(f"{filename} line {call.lineno}: unsupported task type {dagon_type}.")
            try:
                args = [resolver.value(arg) for arg in call.args[1:]]
                kwargs = {keyword.arg: resolver.value(keyword.value) for keyword in call.keywords if keyword.arg}
            except Exception as exc:
                raise PythonImportError(
                    f"{filename} line {call.lineno}: DagonTask arguments must be literals or simple constants ({exc.__class__.__name__}). "
                    "Rewrite computed arguments as literals before importing."
                ) from None
            try:
                task_name, config = config_from_arguments(dagon_type, args, kwargs)
            except TypeError as exc:
                raise PythonImportError(f"{filename} line {call.lineno}: {exc}") from None
            ancestor = parents.get(call)
            while ancestor is not None and not isinstance(ancestor, ast.stmt):
                ancestor = parents.get(ancestor)
            loop = ancestor
            while loop is not None and not isinstance(loop, (ast.For, ast.While, ast.ListComp)):
                loop = parents.get(loop)
            if loop is not None:
                warnings.append(f"line {call.lineno}: DagonTask inside a loop was imported once; expand loops into separate tasks.")
            variable = None
            if isinstance(ancestor, ast.Assign) and len(ancestor.targets) == 1 and isinstance(ancestor.targets[0], ast.Name):
                variable = ancestor.targets[0].id
            line = ancestor.lineno if ancestor is not None else call.lineno
            comment, layout = _leading_comment(lines, comments, line)
            task = ParsedTask(variable, task_name, dagon_type, config, call.lineno, comment, layout, (args, kwargs))
            tasks.append(task)
            if variable:
                by_variable[variable] = task
        elif name == "add_dependency_to" and isinstance(call.func, ast.Attribute) and call.args:
            target = call.func.value
            source = call.args[0]
            if isinstance(target, ast.Name) and isinstance(source, ast.Name):
                explicit.append((target.id, source.id))
    return {
        "workflow_name": workflow_name,
        "options": workflow_options,
        "tasks": tasks,
        "by_variable": by_variable,
        "explicit": explicit,
        "constants": constants,
        "docstring": ast.get_docstring(tree) or "",
        "warnings": warnings,
    }


_GENERATED_DOC = re.compile(r"^.*: a DAGonStar workflow\.\n?\n?", re.S)


def _clean_docstring(docstring: str) -> str:
    """Drop the boilerplate DAGonWeb adds, keep the author's text."""
    text = docstring
    if "Generated by DAGonWeb" in text:
        head = text.split("Generated by DAGonWeb", 1)[0]
        text = _GENERATED_DOC.sub("", head, count=1) if head.strip() else ""
        text = re.sub(r"^[^\n]*: a DAGonStar workflow\.\s*", "", text)
    return text.strip()


def read_upload(filename: str, content: bytes) -> dict[str, bytes]:
    """Return ``{path: bytes}`` for a ``.py`` or ``.zip`` upload, safely."""
    if filename.lower().endswith(".py"):
        if len(content) > MAX_SOURCE_BYTES:
            raise PythonImportError("The Python file is too large.")
        return {PurePosixPath(filename).name: content}
    if not zipfile.is_zipfile(io.BytesIO(content)):
        raise PythonImportError("Upload a .py file or a .zip archive.")
    files: dict[str, bytes] = {}
    total = 0
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        members = [info for info in archive.infolist() if not info.is_dir()]
        if len(members) > MAX_ARCHIVE_FILES:
            raise PythonImportError("The archive has too many files.")
        for info in members:
            path = PurePosixPath(info.filename)
            if path.is_absolute() or ".." in path.parts or "__MACOSX" in path.parts or info.flag_bits & 0x1:
                continue
            if path.suffix.lower() not in (".py", ".json", ".ini", ".txt", ".cfg", ".md"):
                continue
            total += info.file_size
            if info.file_size > MAX_SOURCE_BYTES or total > MAX_ARCHIVE_BYTES:
                raise PythonImportError("The archive is too large once extracted.")
            files[str(path)] = archive.read(info)
    if not files:
        raise PythonImportError("The archive contains no Python or JSON files.")
    return files


def _strip_common_root(files: dict[str, bytes]) -> dict[str, bytes]:
    roots = {PurePosixPath(path).parts[0] for path in files if len(PurePosixPath(path).parts) > 1}
    if len(roots) == 1 and all(len(PurePosixPath(path).parts) > 1 for path in files):
        root = roots.pop()
        return {str(PurePosixPath(*PurePosixPath(path).parts[1:])): data for path, data in files.items()}
    return files


def _main_program(sources: dict[str, str]) -> str:
    candidates = [path for path, text in sources.items() if "DagonTask" in text and "Workflow" in text]
    if not candidates:
        raise PythonImportError("No DAGonStar program found: expected Workflow(...) and DagonTask(...) calls.")
    if len(candidates) == 1:
        return candidates[0]
    preferred = [path for path in candidates if PurePosixPath(path).name in ("workflow.py", "main.py", "__main__.py", "app.py")]
    if len(preferred) == 1:
        return preferred[0]
    shallow = sorted(candidates, key=lambda path: (len(PurePosixPath(path).parts), path))
    if len(PurePosixPath(shallow[0]).parts) < len(PurePosixPath(shallow[1]).parts):
        return shallow[0]
    raise PythonImportError("Several DAGonStar programs found: " + ", ".join(sorted(candidates)) + ". Upload one at a time.")


def import_python_files(files: dict[str, bytes], fallback_name: str = "imported_workflow") -> tuple[dict[str, Any], list[str]]:
    """Convert a DAGonStar application into a DAGonWeb document.

    Returns ``(document, warnings)``. The document uses the editor format
    accepted by ``apply_workflow_document``.
    """
    files = _strip_common_root(files)
    sources: dict[str, str] = {}
    for path, data in files.items():
        if path.endswith(".py"):
            try:
                sources[path] = data.decode("utf-8")
            except UnicodeDecodeError:
                raise PythonImportError(f"{path} is not UTF-8 text.") from None
    if not any("DagonTask" in text for text in sources.values()):
        documents = [path for path in files if path.endswith(".json")]
        for path in documents:
            try:
                document = json.loads(files[path])
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(document, dict) and isinstance(document.get("tasks"), dict):
                return document, [f"Imported the DAGonStar document {path}."]
        raise PythonImportError("No DAGonStar program found: expected Workflow(...) and DagonTask(...) calls.")
    main = _main_program(sources)
    parsed = _parse_program(sources[main], main)
    warnings = list(parsed["warnings"])
    if not parsed["tasks"]:
        raise PythonImportError(f"{main} declares no DagonTask.")
    constants = parsed["constants"]
    interactive = constants.get("INTERACTIVE_TASKS") if isinstance(constants.get("INTERACTIVE_TASKS"), dict) else {}
    native_sources = constants.get("NATIVE_SOURCES") if isinstance(constants.get("NATIVE_SOURCES"), dict) else {}
    requirements = files.get("requirements.txt", b"").decode("utf-8", "replace")
    names: set[str] = set()
    tasks: dict[str, dict[str, Any]] = {}
    comments: dict[str, str] = {}
    for task in parsed["tasks"]:
        if task.name in names:
            raise PythonImportError(f"{main} line {task.line}: duplicate task name {task.name}.")
        names.add(task.name)
        task_type, config = task.task_type, task.config
        recorded = task.layout.get("config")
        if isinstance(recorded, dict):
            try:
                expected = dagon_task_arguments(task_type, task.name, recorded)
            except Exception:
                expected = None
            if expected is not None and json.dumps(expected, sort_keys=True, default=str) == json.dumps((task.arguments[0], task.arguments[1]), sort_keys=True, default=str):
                config = recorded  # arguments unchanged since export
        restored = interactive.get(task.name)
        if isinstance(restored, dict) and restored.get("kind") in INTERACTIVE_TYPES and task_type == "batch":
            task_type, config = restored["kind"], dict(restored.get("config") or {})
        if task_type == "native":
            module = str(config.get("callable", "")).partition(":")[0]
            module_path = module.replace(".", "/") + ".py"
            if module in native_sources:
                config["source"] = str(native_sources[module])
            else:
                match = next((path for path in sources if path == module_path or path.endswith("/" + module_path)), None)
                if match:
                    config["source"] = sources[match]
                else:
                    warnings.append(f"Native task {task.name}: module {module} is not in the upload; it must be importable at run time.")
            if requirements.strip() and config.get("executor", "local") == "local" and not config.get("requirements"):
                kept = [line for line in requirements.splitlines() if line.strip() and not line.lower().startswith("dagonstar") and not line.startswith("#")]
                if kept:
                    config["requirements"] = "\n".join(kept) + "\n"
        tasks[task.name] = {
            "name": task.name,
            "type": task_type,
            "command": "",
            "explicit_prevs": [],
            "dagonweb": {"config": config, **({"x": int(task.layout["x"]), "y": int(task.layout["y"])} if isinstance(task.layout.get("x"), (int, float)) and isinstance(task.layout.get("y"), (int, float)) else {})},
        }
        if task.comment:
            comments[task.name] = task.comment
    for target, source in parsed["explicit"]:
        target_task, source_task = parsed["by_variable"].get(target), parsed["by_variable"].get(source)
        if target_task and source_task and source_task.name not in tasks[target_task.name]["explicit_prevs"]:
            tasks[target_task.name]["explicit_prevs"].append(source_task.name)
    name = parsed["workflow_name"] or constants.get("WORKFLOW_NAME") or fallback_name
    name = re.sub(r"[^A-Za-z0-9_-]+", "_", str(name)).strip("_")[:80] or fallback_name
    metadata = constants.get("DAGONWEB_METADATA") if isinstance(constants.get("DAGONWEB_METADATA"), dict) else {}
    metadata = dict(metadata)
    runtime = dict(metadata.get("runtime") or {})
    runtime.update({key: value for key, value in parsed["options"].items() if key in ("portable_emulation", "pre_stage", "max_threads")})
    if runtime:
        metadata["runtime"] = runtime
    metadata["python"] = {"source_file": main, "docstring": _clean_docstring(parsed["docstring"]), "comments": comments}
    document: dict[str, Any] = {"name": name, "tasks": tasks, "dagonweb": metadata}
    if "dagon.ini" in files:
        try:
            document["dagon_ini"] = parse_dagon_ini(files["dagon.ini"].decode("utf-8"))
        except Exception:
            warnings.append("dagon.ini could not be parsed and was ignored.")
    elif isinstance(constants.get("EXTRA_SECTIONS"), dict) and constants["EXTRA_SECTIONS"]:
        document["dagon_ini"] = constants["EXTRA_SECTIONS"]
    references = [match.group("task") for value in tasks.values() for match in WORKFLOW_REFERENCE.finditer(json.dumps(value["dagonweb"]["config"])) if match.group("workflow") not in ("", name)]
    if references:
        warnings.append("Some workflow:// references point to other workflows: " + ", ".join(sorted(set(references))))
    return document, warnings
