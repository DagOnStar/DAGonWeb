#!/usr/bin/env python3
"""dagonweb-runner: the only program DAGonWeb may run on the HPC login node.

Install it once, owned by root and not writable by users, next to a Python
that has DAGonStar installed::

    install -o root -g root -m 0755 hpc/dagonweb_runner.py /opt/dagonweb/bin/dagonweb-runner

Each user links their DAGonWeb account by adding one restricted line to
``~/.ssh/authorized_keys``::

    restrict,from="172.16.0.0/12",command="/opt/dagonweb/bin/dagonweb-runner" ssh-ed25519 AAAA... dagonweb-account-42

``command=`` forces sshd to start this program whatever the client asks for.
The requested command arrives in ``SSH_ORIGINAL_COMMAND`` and is parsed here
against a fixed set of verbs; nothing is ever handed to a shell. The runner
always executes as the user who owns the key, so it can never do more than
that user can, and it only touches files below its base directory
(``$SCRATCH/dagonweb``, else ``~/dagonweb``, or ``base`` in
``/etc/dagonweb/runner.json``).

Verbs (one JSON document on stdout unless noted)::

    ping                         identity, base directory, Python, DAGonStar version
    submit <run> [--resume]      read a run spec (JSON) on stdin, start it detached
    status <run> [<offset>]      state plus the events written since <offset>
    cancel <run>                 stop a running run
    ls <run> [<path>]            list a directory of the run
    get <run> <path> [<max>]     raw file bytes on stdout
    archive <run> [<path>] [<max>]  ZIP of a run folder on stdout
    put <run> <path>             write stdin to a file below _dagonweb/data/ (interactive inputs)
    delete <run>                 remove the run directory
    keys                         the user's own authorized keys (for SSH-signature sign-in)

The DAGonStar orchestrator runs detached on the login node, or, with
``orchestrator: slurm`` (in the run spec or forced in runner.json), as a
small Slurm job submitted with ``sbatch``.

The module uses the standard library only (plus DAGonStar for ``submit``),
so it can be copied to the cluster without the rest of DAGonWeb.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import venv
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

RUNNER_VERSION = "2"
CONFIG_FILE = Path(os.environ.get("DAGONWEB_RUNNER_CONFIG", "/etc/dagonweb/runner.json"))
REMOTE_VERBS = {"ping", "submit", "status", "cancel", "ls", "get", "archive", "put", "delete", "keys"}
RUN_ID = re.compile(r"^[0-9]{1,12}$")
TASK_NAME = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
MAX_SPEC_BYTES = 20 * 1024 * 1024
MAX_EVENTS_BYTES = 1024 * 1024
DEFAULT_MAX_GET = 1024 * 1024 * 1024
DEFAULT_MAX_PUT = 1024 * 1024 * 1024
DATA_DIR = ("_dagonweb", "data")
ACTIVE_STATES = {"starting", "queued", "running"}
SLURM_DONE = {"", "COMPLETED", "FAILED", "CANCELLED", "TIMEOUT", "OUT_OF_MEMORY", "NODE_FAIL", "PREEMPTED", "BOOT_FAIL", "DEADLINE"}
KEY_TYPES = ("ssh-ed25519", "ssh-rsa", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521", "sk-ssh-ed25519@openssh.com", "sk-ecdsa-sha2-nistp256@openssh.com")
STATE_DIR = "_dagonweb"
EVENTS = "events.jsonl"
STATE = "state.json"
SPEC = "spec.json"
TASK_EVENTS = {
    "on_task_staging_in_start": "staging inputs",
    "on_task_execute_start": "executing",
    "on_task_staging_out_start": "staging outputs",
}


class RunnerError(Exception):
    """A refused or failed request; reported as ``{"ok": false, ...}``."""

    def __init__(self, message: str, code: int = 2) -> None:
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------- configuration


def load_config() -> dict[str, Any]:
    try:
        value = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise RunnerError(f"Unreadable runner configuration {CONFIG_FILE}: {exc}", 70) from exc
    return value if isinstance(value, dict) else {}


def base_dir(config: dict[str, Any]) -> Path:
    configured = str(config.get("base") or "")
    if configured:
        base = Path(os.path.expanduser(os.path.expandvars(configured)))
    elif os.environ.get("DAGONWEB_RUNNER_BASE"):
        base = Path(os.environ["DAGONWEB_RUNNER_BASE"])
    elif os.environ.get("SCRATCH"):
        base = Path(os.environ["SCRATCH"]) / "dagonweb"
    else:
        base = Path.home() / "dagonweb"
    if not base.is_absolute():
        raise RunnerError("The runner base directory must be an absolute path.", 70)
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    return base.resolve()


def username() -> str:
    try:
        import pwd

        return pwd.getpwuid(os.getuid()).pw_name
    except (ImportError, KeyError):  # pragma: no cover - non-POSIX
        return os.environ.get("USER", "")


def check_account(config: dict[str, Any]) -> None:
    minimum = int(config.get("min_uid", 1000))
    if hasattr(os, "getuid") and os.getuid() < minimum:
        raise RunnerError(f"dagonweb-runner refuses to run for uid {os.getuid()} (below {minimum}).", 77)


# ---------------------------------------------------------------- paths


def run_dir(base: Path, run_id: str) -> Path:
    if not RUN_ID.match(run_id):
        raise RunnerError("Invalid run id.")
    return base / f"run_{run_id}"


def inside(root: Path, relative: str) -> Path:
    """Resolve ``relative`` below ``root``; symbolic links may not escape it."""
    if "\0" in relative or len(relative) > 4096:
        raise RunnerError("Invalid path.")
    root = root.resolve()
    path = (root / relative.lstrip("/")).resolve()
    if path != root and root not in path.parents:
        raise RunnerError("Path outside the run directory.", 13)
    return path


def state_path(directory: Path, name: str) -> Path:
    return directory / STATE_DIR / name


def read_state(directory: Path) -> dict[str, Any]:
    try:
        value = json.loads(state_path(directory, STATE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def write_state(directory: Path, **values: Any) -> None:
    state = read_state(directory)
    state.update(values)
    target = state_path(directory, STATE)
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(state), encoding="utf-8")
    temporary.replace(target)


def alive(pid: Any) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - someone else's pid
        return False
    return True


def job_alive(job_id: Any) -> bool:
    """Whether a Slurm job is still queued or running."""
    if not str(job_id).isdigit():
        return False
    try:
        done = subprocess.run(["squeue", "-h", "-j", str(job_id), "-o", "%T"], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return True  # cannot tell: never report a live job as lost
    return done.returncode == 0 and done.stdout.strip().upper() not in SLURM_DONE


def is_active(state: dict[str, Any]) -> bool:
    if state.get("state") not in ACTIVE_STATES:
        return False
    if state.get("job_id"):
        return job_alive(state["job_id"])
    return alive(state.get("pid"))


def stop(state: dict[str, Any]) -> None:
    if state.get("job_id"):
        subprocess.run(["scancel", str(state["job_id"])], capture_output=True, timeout=60)
    elif alive(state.get("pid")):
        os.killpg(state["pid"], signal.SIGTERM)


def orchestrator(config: dict[str, Any], spec: dict[str, Any]) -> tuple[str, list[str]]:
    """Where the DAGonStar orchestrator runs, and the extra sbatch options."""
    requested = spec.get("orchestrator") if isinstance(spec.get("orchestrator"), dict) else {}
    mode = config.get("orchestrator") if config.get("orchestrator") in ("login", "slurm") else requested.get("mode")
    options = list(config.get("sbatch_options") or []) + list(requested.get("sbatch_options") or [])
    for option in options:
        if not isinstance(option, str) or not option.startswith("--") or len(option) > 500 or any(char in option for char in "\0\n\r"):
            raise RunnerError(f"Invalid sbatch option {option!r}.")
    return ("slurm" if mode == "slurm" else "login"), options


# ---------------------------------------------------------------- verbs


def verb_ping(base: Path, args: list[str], stdin: Any) -> dict[str, Any]:
    try:
        from importlib.metadata import version

        dagonstar = version("dagonstar")
    except Exception:  # noqa: BLE001 - metadata is best effort
        dagonstar = ""
    return {
        "ok": True,
        "version": RUNNER_VERSION,
        "user": username(),
        "uid": os.getuid() if hasattr(os, "getuid") else -1,
        "base": str(base),
        "python": sys.executable,
        "dagonstar": dagonstar,
    }


def verb_submit(base: Path, args: list[str], stdin: Any) -> dict[str, Any]:
    if not args or len(args) > 2 or (len(args) == 2 and args[1] != "--resume"):
        raise RunnerError("Usage: submit <run> [--resume]")
    resume = len(args) == 2
    directory = run_dir(base, args[0])
    raw = stdin.read(MAX_SPEC_BYTES + 1)
    if len(raw) > MAX_SPEC_BYTES:
        raise RunnerError("The run specification is too large.")
    try:
        spec = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise RunnerError(f"The run specification is not valid JSON: {exc}") from exc
    validate_spec(spec)
    (directory / STATE_DIR).mkdir(mode=0o700, parents=True, exist_ok=True)
    state = read_state(directory)
    if is_active(state):
        raise RunnerError("This run is already running.", 16)
    if not resume and state:
        raise RunnerError("This run already exists; resume it instead.", 17)
    mode, options = orchestrator(load_config(), spec)
    state_path(directory, SPEC).write_text(json.dumps(spec), encoding="utf-8")
    state_path(directory, EVENTS).write_text("", encoding="utf-8")
    write_state(directory, state="starting", resume=resume, submitted_at=now(), pid=None, job_id=None, finished_at=None, error="", orchestrator=mode)
    command = [sys.executable, "-I", str(Path(__file__).resolve()), "_execute", args[0]] + (["--resume"] if resume else [])
    environment = {**os.environ, "DAGONWEB_RUNNER_BASE": str(base)}
    log_file = state_path(directory, "runner.log")
    if mode == "slurm":
        done = subprocess.run(
            ["sbatch", "--parsable", f"--job-name=dagonweb-run-{args[0]}", f"--chdir={directory}", f"--output={log_file}", "--export=ALL", *options, "--wrap", shlex.join(command)],
            capture_output=True, text=True, env=environment, timeout=120,
        )
        job_id = done.stdout.strip().split(";")[0]
        if done.returncode or not job_id.isdigit():
            write_state(directory, state="failed", error=f"sbatch failed: {(done.stderr or done.stdout).strip()[-2000:]}", finished_at=now())
            raise RunnerError(f"sbatch failed: {(done.stderr or done.stdout).strip()[-2000:]}")
        write_state(directory, state="queued", job_id=int(job_id))
        events = EventLog(state_path(directory, EVENTS))
        events.write(run="queued", job_id=int(job_id))
        events.close()
        return {"ok": True, "run_dir": str(directory), "job_id": int(job_id)}
    log = log_file.open("ab")
    process = subprocess.Popen(command, cwd=str(directory), stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True, close_fds=True, env=environment)
    log.close()
    write_state(directory, pid=process.pid)
    return {"ok": True, "run_dir": str(directory), "pid": process.pid}


def verb_status(base: Path, args: list[str], stdin: Any) -> dict[str, Any]:
    if not args or len(args) > 2:
        raise RunnerError("Usage: status <run> [<offset>]")
    directory = run_dir(base, args[0])
    offset = int(args[1]) if len(args) == 2 and args[1].isdigit() else 0
    state = read_state(directory)
    if not state:
        return {"ok": True, "state": "unknown", "events": [], "offset": offset}
    current = state.get("state", "unknown")
    if current in ACTIVE_STATES and (state.get("pid") or state.get("job_id")) and not is_active(state):
        # It may have finished a moment ago: read the state once more.
        state = read_state(directory)
        current = state.get("state", "unknown")
        if current in ACTIVE_STATES:
            current = "lost"  # killed from outside (OOM killer, reboot, scancel, limits)
    events: list[dict[str, Any]] = []
    try:
        with state_path(directory, EVENTS).open("rb") as handle:
            handle.seek(offset)
            chunk = handle.read(MAX_EVENTS_BYTES)
    except OSError:
        chunk = b""
    end = chunk.rfind(b"\n") + 1
    for line in chunk[:end].splitlines():
        try:
            events.append(json.loads(line))
        except ValueError:
            continue
    return {"ok": True, "state": current, "job_id": state.get("job_id"), "error": state.get("error", ""), "events": events, "offset": offset + end, "more": end < len(chunk) or len(chunk) == MAX_EVENTS_BYTES}


def verb_cancel(base: Path, args: list[str], stdin: Any) -> dict[str, Any]:
    if len(args) != 1:
        raise RunnerError("Usage: cancel <run>")
    directory = run_dir(base, args[0])
    state = read_state(directory)
    if is_active(state):
        stop(state)
        write_state(directory, state="failed", error="Cancelled.", finished_at=now())
        return {"ok": True, "cancelled": True}
    return {"ok": True, "cancelled": False}


def verb_ls(base: Path, args: list[str], stdin: Any) -> dict[str, Any]:
    if not args or len(args) > 2:
        raise RunnerError("Usage: ls <run> [<path>]")
    root = run_dir(base, args[0])
    target = inside(root, args[1] if len(args) == 2 else "")
    if not target.exists():
        return {"ok": True, "files": []}
    if not target.is_dir():
        raise RunnerError("Path is not a directory.")
    files = []
    for child in sorted(target.iterdir()):
        resolved = child.resolve()
        if resolved != root.resolve() and root.resolve() not in resolved.parents:
            continue
        stat = child.stat()
        files.append({"name": child.name, "is_dir": child.is_dir(), "size": stat.st_size, "modified_at": stat.st_mtime})
    return {"ok": True, "files": files}


def verb_get(base: Path, args: list[str], stdin: Any) -> None:
    if len(args) not in (2, 3):
        raise RunnerError("Usage: get <run> <path> [<max bytes>]")
    target = inside(run_dir(base, args[0]), args[1])
    limit = int(args[2]) if len(args) == 3 and args[2].isdigit() else DEFAULT_MAX_GET
    if not target.is_file():
        raise RunnerError("Path is not a file.", 44)
    if target.stat().st_size > limit:
        raise RunnerError("File is too large.", 27)
    out = sys.stdout.buffer
    with target.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            out.write(chunk)
    out.flush()
    return None


def verb_delete(base: Path, args: list[str], stdin: Any) -> dict[str, Any]:
    if len(args) != 1:
        raise RunnerError("Usage: delete <run>")
    directory = run_dir(base, args[0])
    if is_active(read_state(directory)):
        raise RunnerError("The run is still active.", 16)
    if directory.is_dir() and not directory.is_symlink():
        shutil.rmtree(directory)
    return {"ok": True}


def verb_archive(base: Path, args: list[str], stdin: Any) -> None:
    if not args or len(args) > 3:
        raise RunnerError("Usage: archive <run> [<path>] [<max bytes>]")
    root = run_dir(base, args[0]).resolve()
    target = inside(root, args[1] if len(args) > 1 else "")
    limit = int(args[2]) if len(args) == 3 and args[2].isdigit() else DEFAULT_MAX_GET
    if not target.is_dir():
        raise RunnerError("Path is not a directory.", 44)
    files, total = [], 0
    for path in sorted(target.rglob("*")):
        resolved = path.resolve()
        if path.is_file() and (resolved == root or root in resolved.parents):
            files.append(path)
            total += path.stat().st_size
            if total > limit:
                raise RunnerError("The folder is too large to archive.", 27)
    with zipfile.ZipFile(sys.stdout.buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(path, str(path.relative_to(target)))
    sys.stdout.buffer.flush()
    return None


def verb_put(base: Path, args: list[str], stdin: Any) -> dict[str, Any]:
    """Store an interactive input (Upload/Form tasks) below ``_dagonweb/data``."""
    if len(args) != 2:
        raise RunnerError("Usage: put <run> <path>")
    root = run_dir(base, args[0])
    if not root.is_dir():
        raise RunnerError("Unknown run.", 44)
    data_root = (root / Path(*DATA_DIR)).resolve()
    target = inside(root, args[1])
    if data_root not in target.parents:
        raise RunnerError("Inputs can only be written below _dagonweb/data/.", 13)
    limit = int(load_config().get("max_put_bytes") or DEFAULT_MAX_PUT)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.part")
    written = 0
    with temporary.open("wb") as handle:
        while chunk := stdin.read(1024 * 1024):
            written += len(chunk)
            if written > limit:
                handle.close()
                temporary.unlink()
                raise RunnerError("The input is too large.", 27)
            handle.write(chunk)
    temporary.replace(target)
    return {"ok": True, "size": written}


def split_key_line(line: str) -> tuple[str, str] | None:
    """Split an authorized_keys line into (options, "type base64")."""
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    options, index, quoted = "", 0, False
    if not line.startswith(KEY_TYPES):
        while index < len(line) and (quoted or not line[index].isspace()):
            if line[index] == '"' and (index == 0 or line[index - 1] != "\\"):
                quoted = not quoted
            index += 1
        options, line = line[:index], line[index:].strip()
    parts = line.split()
    if len(parts) < 2 or parts[0] not in KEY_TYPES:
        return None
    return options, f"{parts[0]} {parts[1]}"


def verb_keys(base: Path, args: list[str], stdin: Any) -> dict[str, Any]:
    """The user's own public keys, so DAGonWeb can check an SSH signature."""
    if args:
        raise RunnerError("Usage: keys")
    config = load_config()
    files = config.get("authorized_keys_files") or ["~/.ssh/authorized_keys", "~/.ssh/authorized_keys2"]
    home, user = str(Path.home()), username()
    keys = []
    for pattern in files:
        path = Path(os.path.expanduser(str(pattern).replace("%h", home).replace("%u", user).replace("%%", "%")))
        try:
            content = path.read_text(encoding="utf-8", errors="replace")[:1_000_000]
        except OSError:
            continue
        for raw in content.splitlines():
            parsed = split_key_line(raw)
            if parsed:
                keys.append({"options": parsed[0], "key": parsed[1]})
    return {"ok": True, "keys": keys[:500]}


VERBS = {"ping": verb_ping, "submit": verb_submit, "status": verb_status, "cancel": verb_cancel, "ls": verb_ls, "get": verb_get, "archive": verb_archive, "put": verb_put, "delete": verb_delete, "keys": verb_keys}


# ---------------------------------------------------------------- run specification


def validate_spec(spec: Any) -> None:
    if not isinstance(spec, dict) or not isinstance(spec.get("name"), str) or not isinstance(spec.get("tasks"), list):
        raise RunnerError("The run specification needs a name and a task list.")
    names = set()
    for task in spec["tasks"]:
        if not isinstance(task, dict) or not TASK_NAME.match(str(task.get("name", ""))):
            raise RunnerError("Every task needs a valid name.")
        if not isinstance(task.get("type"), str) or not isinstance(task.get("args"), list) or not isinstance(task.get("kwargs", {}), dict):
            raise RunnerError(f"Task {task['name']} has an invalid definition.")
        names.add(task["name"])
    for edge in spec.get("explicit", []):
        if not (isinstance(edge, list) and len(edge) == 2 and set(edge) <= names):
            raise RunnerError("Explicit dependencies must name two tasks of the run.")
    for item in spec.get("native", []):
        if not isinstance(item, dict) or item.get("task") not in names:
            raise RunnerError("Native sources must belong to a task of the run.")
        module = str(item.get("module") or "")
        if module and not all(part.isidentifier() for part in module.split(".")):
            raise RunnerError("Invalid Native module name.")
    if not isinstance(spec.get("config", {}), dict):
        raise RunnerError("The DAGonStar configuration must be an object.")


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class EventLog:
    """Append-only JSON-lines event file, safe for DAGonStar's task threads."""

    def __init__(self, path: Path) -> None:
        self.lock = threading.Lock()
        self.handle = path.open("a", encoding="utf-8")

    def write(self, **event: Any) -> None:
        event.setdefault("at", now())
        with self.lock:
            self.handle.write(json.dumps(event) + "\n")
            self.handle.flush()

    def close(self) -> None:
        self.handle.close()


class FailureCapture(logging.Handler):
    """Keep the exception DAGonStar logs when a task thread fails."""

    def __init__(self, tasks: list[Any]) -> None:
        super().__init__(level=logging.ERROR)
        self.tasks = {id(task): task for task in tasks}
        self.failures: dict[str, str] = {}

    def emit(self, record: logging.LogRecord) -> None:
        task = self.tasks.get(id(threading.current_thread()))
        if task is not None and record.exc_info and record.exc_info[1] is not None:
            self.failures[task.name] = str(record.exc_info[1]).strip()[-4000:]


def task_status(task: Any) -> str:
    return {"RUNNING": "running", "FINISHED": "success", "FAILED": "failed"}.get(getattr(getattr(task, "status", None), "name", ""), "pending")


def failure_detail(task: Any, failures: dict[str, str]) -> str:
    if failures.get(task.name):
        return failures[task.name]
    if any(task_status(previous) == "failed" for previous in getattr(task, "prevs", [])):
        return "skipped because an upstream task failed"
    result = getattr(task, "result", None)
    if isinstance(result, dict) and str(result.get("message") or "").strip():
        return str(result["message"]).strip()[-4000:]
    return ""


def prepare_native(spec: dict[str, Any], directory: Path) -> tuple[Path | None, dict[str, dict[str, Any]]]:
    """Write embedded Native sources and build per-task virtual environments."""
    module_root = directory / STATE_DIR / "native_modules"
    overrides: dict[str, dict[str, Any]] = {}
    has_modules = False
    for item in spec.get("native", []):
        module, source = str(item.get("module") or ""), item.get("source")
        if module and isinstance(source, str) and source.strip():
            parts = module.split(".")
            target = inside(module_root.parent, str(Path("native_modules", *parts[:-1], f"{parts[-1]}.py")))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(source.rstrip() + "\n", encoding="utf-8")
            parent = module_root
            for part in parts[:-1]:
                parent = parent / part
                (parent / "__init__.py").touch(exist_ok=True)
            has_modules = True
        requirements = item.get("requirements")
        if isinstance(requirements, str) and requirements.strip():
            task_root = directory / STATE_DIR / "native_environments" / item["task"]
            task_root.mkdir(parents=True, exist_ok=True)
            (task_root / "requirements.txt").write_text(requirements.rstrip() + "\n", encoding="utf-8")
            python = task_root / "venv" / "bin" / "python"
            if not python.is_file():
                venv.EnvBuilder(with_pip=True, system_site_packages=True).create(task_root / "venv")
            completed = subprocess.run([str(python), "-m", "pip", "install", "--disable-pip-version-check", "--no-input", "-r", str(task_root / "requirements.txt")], text=True, capture_output=True)
            if completed.returncode:
                raise RuntimeError(f"Native task {item['task']} requirements installation failed.\n{(completed.stdout + completed.stderr).strip()}")
            overrides[item["task"]] = {"python": str(python)}
    return (module_root if has_modules else None), overrides


def enable_fair(runtime: Any, spec: dict[str, Any], directory: Path) -> None:
    """Record FAIR provenance with DAGonStar when the workflow asks for it."""
    profile = spec.get("fair")
    if not isinstance(profile, dict) or not profile.get("enabled"):
        return
    from dagon.fair import AccessPolicy, Agent, FairProfile

    policy = profile.get("access_policy") or "local"
    runtime.enable_fair(FairProfile(
        title=profile.get("title") or spec["name"],
        description=profile.get("description") or "",
        creators=[Agent(name=str(name)) for name in profile.get("creators") or [] if name],
        license=profile.get("license") or "",
        keywords=list(profile.get("keywords") or []),
        access_policy=AccessPolicy(metadata=policy, data=policy),
        strict=bool(profile.get("strict")),
        output_dir=str(directory / ".dagon" / "fair" / spec["name"]),
        capture_environment=list(profile.get("environment_allowlist") or []) if profile.get("capture_environment") else [],
    ))


def execute(base: Path, run_id: str, resume: bool) -> int:
    """Run a submitted specification with DAGonStar (detached child process)."""
    directory = run_dir(base, run_id)
    spec = json.loads(state_path(directory, SPEC).read_text(encoding="utf-8"))
    events = EventLog(state_path(directory, EVENTS))
    write_state(directory, state="running", pid=os.getpid(), started_at=now())
    (directory / STATE_DIR / "data").mkdir(parents=True, exist_ok=True)
    capture: FailureCapture | None = None
    final, error = "failed", ""
    try:
        from dagon import Workflow
        from dagon.task import DagonTask, TaskType

        config = spec.get("config") or {}
        # Task directories never leave the run directory, whatever the spec says.
        config.setdefault("batch", {}).update({"scratch_dir_base": str(directory), "remove_dir": False})
        module_root, overrides = prepare_native(spec, directory)
        if module_root:
            sys.path.insert(0, str(module_root))
        options = spec.get("runtime") or {}
        checkpoint = directory / STATE_DIR / "checkpoint.json"
        runtime = Workflow(
            spec["name"],
            config=config,
            max_threads=max(1, min(int(options.get("max_threads") or 10), 256)),
            checkpoint_file=str(checkpoint),
            portable_emulation=bool(options.get("portable_emulation")),
            pre_stage=bool(options.get("pre_stage")),
        )
        tasks: dict[str, Any] = {}
        for item in spec["tasks"]:
            kwargs = dict(item.get("kwargs") or {})
            kwargs.update(overrides.get(item["name"], {}))
            task = DagonTask(TaskType[item["type"].upper()], *item["args"], **kwargs)
            runtime.add_task(task)
            tasks[item["name"]] = task
        for target, source in spec.get("explicit", []):
            tasks[target].add_dependency_to(tasks[source])
        runtime.make_dependencies()
        enable_fair(runtime, spec, directory)
        capture = FailureCapture(runtime.tasks)
        logging.getLogger().addHandler(capture)
        seen: dict[str, str] = {}
        lock = threading.Lock()

        def record(task: Any, event: str | None = None) -> None:
            with lock:
                status = task_status(task)
                changed = seen.get(task.name) != status
                if not event and not changed:
                    return
                seen[task.name] = status
                detail = failure_detail(task, capture.failures) if changed and status == "failed" else ""
            events.write(task=task.name, status=status, event=event or "", working_dir=str(getattr(task, "working_dir", "") or ""), error=detail)

        runtime.on_task_start += lambda task: record(task)
        runtime.on_task_end += lambda task: record(task)
        for name, label in TASK_EVENTS.items():
            runtime.add_listener(name, lambda task, label=label: record(task, label))
        resume_file = str(checkpoint) if resume and checkpoint.is_file() else None
        events.write(run="started", resumed=bool(resume_file))
        runtime.launch(resume_checkpoint_file=resume_file)
        runtime.wait()
        for task in runtime.tasks:
            record(task)
        final = "success" if all(task.status.name == "FINISHED" for task in runtime.tasks) else "failed"
    except Exception as exc:  # noqa: BLE001 - reported to DAGonWeb
        error = str(exc)[-4000:] or exc.__class__.__name__
    finally:
        if capture is not None:
            logging.getLogger().removeHandler(capture)
    events.write(run="finished", status=final, error=error)
    events.close()
    write_state(directory, state=final, error=error, finished_at=now())
    return 0


# ---------------------------------------------------------------- entry point


def requested_command(argv: list[str]) -> tuple[list[str], bool]:
    """Return the requested words and whether they came over SSH."""
    if argv:
        return argv, False
    original = os.environ.get("SSH_ORIGINAL_COMMAND")
    if original is None:
        raise RunnerError("dagonweb-runner is meant to be started by sshd as a forced command.", 64)
    try:
        words = shlex.split(original)
    except ValueError as exc:
        raise RunnerError(f"Unparsable command: {exc}", 64) from exc
    if words and Path(words[0]).name in ("dagonweb-runner", "dagonweb_runner.py"):
        words = words[1:]
    return words, True


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    try:
        config = load_config()
        check_account(config)
        words, remote = requested_command(argv)
        if not words:
            raise RunnerError("No command given.", 64)
        verb, args = words[0], words[1:]
        base = base_dir(config)
        if verb == "_execute" and not remote:
            return execute(base, args[0], "--resume" in args[1:])
        if verb not in REMOTE_VERBS:
            raise RunnerError(f"Unknown command {verb!r}.", 64)
        result = VERBS[verb](base, args, sys.stdin.buffer)
        if result is not None:
            sys.stdout.write(json.dumps(result) + "\n")
        return 0
    except RunnerError as exc:
        sys.stdout.write(json.dumps({"ok": False, "error": str(exc)}) + "\n")
        return exc.code
    except Exception as exc:  # noqa: BLE001 - never leak a traceback to the client
        sys.stdout.write(json.dumps({"ok": False, "error": f"{exc.__class__.__name__}: {exc}"}) + "\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
