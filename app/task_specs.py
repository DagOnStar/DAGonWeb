"""DAGonStar task-type registry used by DAGonWeb.

One declarative entry per DAGonStar ``TaskType`` drives every place where
DAGonWeb needs to know about a task type:

* the editor palette and inspector forms (``fields`` is serialized to JSON),
* the DAGonStar task-map document (``document_fields``),
* importing plain DAGonStar documents (``config_from_document``),
* the runtime ``DagonTask(...)`` construction (``dagon_task_arguments``), and
* the generated Python program (same arguments, rendered with ``repr``).

The module has no Flask or database dependencies so it can be imported from
models, routes, the executor, and tests alike.
"""
from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass, field
from typing import Any

WORKFLOW_REFERENCE = re.compile(
    r"workflow://(?P<workflow>[A-Za-z0-9_-]{0,80})/(?P<task>[A-Za-z0-9_-]{1,80})/(?P<path>[^\s;|&<>()]+)"
)
SHELL_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# DAGonStar ends a ``workflow://`` reference at the next space only.  Shell
# punctuation glued to a reference would become part of the staged path.
_REFERENCE_TERMINATORS = set(";|&<>)\n\t\r")

COMMAND_TYPES = {"batch", "slurm", "checkpoint", "cloud", "docker", "kubernetes", "apptainer", "nomad"}

CATEGORIES = [
    ("shell", "Shell & HPC"),
    ("containers", "Containers & orchestrators"),
    ("services", "Cloud, web & devices"),
    ("python_ai", "Python & AI"),
]


@dataclass(frozen=True)
class TaskSpec:
    type: str
    label: str
    category: str
    description: str
    icon: str
    default_name: str
    default_config: dict[str, Any]
    fields: list[dict[str, Any]] = field(default_factory=list)
    docs: str = ""

    def public(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "label": self.label,
            "category": self.category,
            "description": self.description,
            "icon": self.icon,
            "default_name": self.default_name,
            "default_config": self.default_config,
            "fields": self.fields,
            "docs": self.docs,
        }


def _f(key: str, label: str, kind: str = "text", **extra: Any) -> dict[str, Any]:
    return {"key": key, "label": label, "kind": kind, **extra}


INPUTS_FIELD = _f(
    "inputs",
    "Named inputs",
    "mapping",
    help="One <code>name = value</code> per line. Values may be <code>workflow:///task/path</code> references; "
    "each name becomes a shell variable (for example <code>$data</code>) holding the staged path.",
)
COMMAND_FIELD = _f(
    "command",
    "Command",
    "code",
    mode="sh",
    help="Shell script run in the task working directory. Reference producer files with "
    "<code>workflow:///task/path</code>; DAGonStar infers the dependency and stages the file.",
)
PRE_STAGE_FIELD = _f(
    "pre_stage",
    "Scheduler-aware pre-staging",
    "tristate",
    advanced=True,
    help="Stage <code>workflow://</code> inputs before requesting scheduler resources. "
    "<em>Workflow default</em> follows the workflow setup.",
)


def _container_fields(image_help: str) -> list[dict[str, Any]]:
    return [_f("image", "Image", help=image_help), COMMAND_FIELD, INPUTS_FIELD]


TASK_SPECS: dict[str, TaskSpec] = {
    spec.type: spec
    for spec in [
        TaskSpec(
            "batch",
            "Batch",
            "shell",
            "Shell script on the DAGonWeb host.",
            "⌘",
            "batch_task",
            {"command": "mkdir -p output\necho hello > output/result.txt", "inputs": {}},
            [COMMAND_FIELD, INPUTS_FIELD],
        ),
        TaskSpec(
            "slurm",
            "Slurm",
            "shell",
            "Batch job submitted with sbatch.",
            "▦",
            "slurm_task",
            {"command": "echo hello from Slurm > output.txt", "partition": "", "time": "01:00:00", "inputs": {}},
            [
                COMMAND_FIELD,
                INPUTS_FIELD,
                _f("partition", "Partition"),
                _f("time", "Time limit", placeholder="HH:MM:SS"),
                _f("nodes", "Nodes", "integer"),
                _f("ntasks", "Tasks", "integer"),
                _f("ntasks_per_node", "Tasks per node", "integer", advanced=True),
                _f("memory", "Memory (MB)", "integer"),
                _f("nodelist", "Node list", advanced=True),
                _f("sbatch_options", "Extra sbatch options", "list", advanced=True, help="One option per line, for example <code>--gres=gpu:1</code>."),
                _f("comment", "Job comment", advanced=True),
                PRE_STAGE_FIELD,
            ],
        ),
        TaskSpec(
            "checkpoint",
            "Checkpoint",
            "shell",
            "Record producer outputs so a failed run can resume.",
            "⚑",
            "checkpoint",
            {"command": "", "inputs": {}},
            [
                _f(
                    "inputs",
                    "Checkpointed references",
                    "mapping",
                    help="One <code>name = workflow:///task/path</code> per line. Each reference is saved by the checkpoint.",
                ),
                _f("command", "Extra checkpoint arguments", advanced=True),
            ],
        ),
        TaskSpec(
            "docker",
            "Docker",
            "containers",
            "Command inside a Docker container.",
            "▣",
            "docker_task",
            {"image": "python:3.12-slim", "command": "python3 -c \"print('hello from Docker')\" > output.txt", "inputs": {}},
            [
                *_container_fields("Docker image, for example <code>python:3.12-slim</code>."),
                _f("volume", "Volume", advanced=True, help="Docker volume specification."),
                _f("remove", "Remove container after run", "checkbox", default=True, advanced=True),
                _f("pull", "Pull image before run", "checkbox", default=True, advanced=True),
                _f("devices", "Devices", "list", advanced=True),
            ],
        ),
        TaskSpec(
            "kubernetes",
            "Kubernetes",
            "containers",
            "Command inside a Kubernetes pod.",
            "☸",
            "kubernetes_task",
            {"image": "ubuntu:22.04", "namespace": "default", "command": "echo hello from Kubernetes > output.txt", "inputs": {}},
            [
                *_container_fields("Container image for the pod."),
                _f("namespace", "Namespace", default="default"),
                _f("privileged", "Privileged pod", "checkbox", advanced=True),
                _f("remove", "Delete pod after run", "checkbox", advanced=True),
                _f("cleanup_timeout", "Cleanup timeout (s)", "integer", advanced=True),
                _f("volumes", "Volumes (JSON)", "json", advanced=True),
                _f("devices", "Devices", "list", advanced=True),
            ],
        ),
        TaskSpec(
            "apptainer",
            "Apptainer",
            "containers",
            "Command inside an Apptainer (Singularity) image.",
            "◫",
            "apptainer_task",
            {"image": "docker://ubuntu:22.04", "command": "echo hello from Apptainer > output.txt", "inputs": {}},
            [
                *_container_fields("Image URI, for example <code>docker://ubuntu:22.04</code> or a .sif path."),
                _f("bind_paths", "Bind paths", "list", advanced=True),
                _f("overlay_size", "Overlay size (MB)", advanced=True),
                _f("remove", "Remove image cache after run", "checkbox", advanced=True),
            ],
        ),
        TaskSpec(
            "nomad",
            "Nomad",
            "containers",
            "Command as a HashiCorp Nomad job.",
            "◈",
            "nomad_task",
            {"image": "ubuntu:22.04", "nomad_address": "http://localhost:4646", "command": "echo hello from Nomad > output.txt", "inputs": {}},
            [
                *_container_fields("Docker image used by the Nomad job."),
                _f("nomad_address", "Nomad address", "url"),
                _f("cpu", "CPU (MHz)", "integer"),
                _f("memory", "Memory (MB)", "integer"),
                _f("datacenter", "Datacenter", advanced=True),
                _f("region", "Region", advanced=True),
                _f("network_mode", "Network mode", advanced=True),
                _f("volume", "Volume", advanced=True),
            ],
        ),
        TaskSpec(
            "cloud",
            "Cloud",
            "services",
            "Command on a cloud instance (libcloud).",
            "☁",
            "cloud_task",
            {"provider": "", "ssh_username": "ubuntu", "command": "echo hello from cloud > output.txt", "inputs": {}},
            [
                COMMAND_FIELD,
                INPUTS_FIELD,
                _f("provider", "Provider", help="DAGonStar cloud provider name; credentials belong in <code>dagon.ini</code>."),
                _f("ssh_username", "SSH user"),
                _f("instance_name", "Instance name", advanced=True),
                _f("instance_id", "Existing instance id", advanced=True),
                _f("instance_flavour", "Instance flavour (JSON)", "json", advanced=True, help="For example <code>{\"image\": \"…\", \"size\": \"…\", \"location\": \"…\"}</code>."),
                _f("key_options", "Key options (JSON)", "json", advanced=True),
                _f("endpoint", "Endpoint", advanced=True),
                _f("stop_instance", "Stop instance after run", "checkbox", advanced=True),
            ],
        ),
        TaskSpec(
            "faas",
            "FaaS",
            "services",
            "Invoke an already-deployed function.",
            "λ",
            "faas_task",
            {
                "provider": "mock",
                "function": "echo",
                "invocation": "sync",
                "inputs": {"value": 4},
                "outputs": {"result": "result.json"},
                "provider_options": {"mock": {"response": {"status": "succeeded", "outputs": {"result": {"value": 8}}}}},
            },
            [
                _f("provider", "Provider", "select", options=["mock", "http", "openwhisk", "openfaas", "knative", "aws_lambda", "azure_functions", "gcp_functions", "fixture"], help="<code>mock</code> runs without credentials."),
                _f("function", "Function"),
                _f("invocation", "Invocation", "select", options=["sync", "async"]),
                _f("inputs", "Inputs (JSON)", "json", help="Nested values may contain <code>workflow:///task/path</code> references."),
                _f("outputs", "Outputs (JSON)", "json", help="Output name → relative file below <code>outputs/</code>."),
                _f("timeout", "Timeout (s)", "integer"),
                _f("profile", "Provider profile", advanced=True),
                _f("provider_options", "Provider options (JSON)", "json", advanced=True, help="Never put secrets here: use <code>dagon.ini</code> credentials."),
                _f("retry", "Retry policy (JSON)", "json", advanced=True),
                _f("completion", "Completion (JSON)", "json", advanced=True),
                _f("input_policy", "Input policy (JSON)", "json", advanced=True),
                _f("output_policy", "Output policy (JSON)", "json", advanced=True),
                _f("annotations", "Annotations (JSON)", "json", advanced=True),
            ],
        ),
        TaskSpec(
            "web",
            "Web",
            "services",
            "HTTP(S) request saved to files.",
            "⇄",
            "web_task",
            {"url": "https://example.com", "method": "GET", "query": {}, "headers": {}, "outputs": {"body": "response.txt"}},
            [
                _f("url", "URL", "url"),
                _f("method", "Method", "select", options=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"]),
                _f("query", "Query parameters", "mapping"),
                _f("headers", "Request headers", "mapping", help="Do not put secrets here."),
                _f("json", "JSON request body", "json"),
                _f("expected_status", "Expected status", "json", placeholder="200 or [200, 201]"),
                _f("outputs", "Response outputs", "mapping", help="<code>body</code>, <code>headers</code> or <code>metadata</code> = relative path."),
                _f("timeout", "Timeout (s)", "number", advanced=True),
                _f("retries", "Retries", "integer", advanced=True),
            ],
        ),
        TaskSpec(
            "iot",
            "IoT",
            "services",
            "Bounded device observation, configuration or actuation.",
            "◉",
            "iot_task",
            {
                "operation": "observe",
                "provider": "mock",
                "target": {"device_id": "station-01"},
                "resource": "temperature",
                "request": {"events": [{"sequence": 1, "value": 20.1, "unit": "Cel"}, {"sequence": 2, "value": 20.4, "unit": "Cel"}]},
                "completion": {"mode": "sample_count", "count": 2, "timeout_seconds": 10},
                "outputs": {"observations": "outputs/observations.json"},
            },
            [
                _f("operation", "Operation", "select", options=["observe", "configure", "actuate", "compute"]),
                _f("provider", "Provider", "select", options=["mock", "http", "mqtt", "coap", "fixture"], help="<code>mock</code> needs no device or network."),
                _f("target", "Target (JSON)", "json"),
                _f("resource", "Resource"),
                _f("action", "Action", advanced=True),
                _f("request", "Request (JSON)", "json"),
                _f("completion", "Completion (JSON)", "json"),
                _f("outputs", "Outputs (JSON)", "json"),
                _f("inputs", "Inputs (JSON)", "json", advanced=True),
                _f("timeout", "Timeout (s)", "number", advanced=True),
                _f("endpoint_ref", "Endpoint reference", advanced=True, help="Name of an endpoint defined in <code>dagon.ini</code>."),
                _f("credential_ref", "Credential reference", advanced=True, help="Name of a credential defined in <code>dagon.ini</code>."),
                _f("provider_options", "Provider options (JSON)", "json", advanced=True),
            ],
        ),
        TaskSpec(
            "native",
            "Native",
            "python_ai",
            "Python function called with staged inputs.",
            "ƒ",
            "native_task",
            {
                "callable": "workflow_functions:run",
                "source": "import json\nfrom pathlib import Path\n\n\ndef run(output_file: str) -> dict:\n    result = {'message': 'hello from native'}\n    path = Path(output_file)\n    path.parent.mkdir(parents=True, exist_ok=True)\n    path.write_text(json.dumps(result, indent=2), encoding='utf-8')\n    return result\n",
                "requirements": "",
                "inputs": {},
                "outputs": {"output_file": "result.json"},
                "executor": "local",
                "resources": {},
                "python": "",
                "environment": {},
            },
            [
                _f("callable", "Callable", placeholder="module:function", help="Importable <code>module:function</code>."),
                _f("source", "Python source", "code", mode="python", help="Optional module source materialized under scratch for the local executor."),
                _f("inputs", "Function inputs", "mapping", help="name = scalar, local file, or <code>workflow:///task/path</code>."),
                _f("outputs", "Declared outputs", "mapping", help="parameter = relative path below <code>outputs/</code>."),
                _f("executor", "Executor", "select", options=["local", "slurm"]),
                _f("requirements", "requirements.txt", "textarea", advanced=True, help="Installed in a task-local virtualenv (local executor only)."),
                _f("resources", "Scheduler resources", "mapping", advanced=True),
                _f("python", "Python interpreter", advanced=True),
                _f("environment", "Environment variables", "mapping", advanced=True, help="Do not put secrets here."),
            ],
        ),
        TaskSpec(
            "llm",
            "LLM",
            "python_ai",
            "OpenAI-compatible chat completion.",
            "✦",
            "llm_task",
            {
                "provider": "openai-compatible",
                "prompt": {"messages": [{"role": "user", "content": "Summarize: {input}"}]},
                "params": {"input": "DAGonStar runs workflows from laptops to HPC clusters and clouds."},
                "input_files": {},
                "output_file": "response.json",
                "timeout": 120,
            },
            [
                _f("provider", "Provider", help="Credentials belong in the <code>[llm.&lt;provider&gt;]</code> section of <code>dagon.ini</code>."),
                _f("prompt", "Request JSON", "json", help="Object with a <code>messages</code> array; put <code>model</code> and options here. Use <code>{name}</code> placeholders."),
                _f("params", "Prompt parameters", "mapping"),
                _f("input_files", "Staged inputs", "mapping", help="placeholder = <code>workflow:///task/path</code>."),
                _f("output_file", "Response file"),
                _f("timeout", "Timeout (s)", "integer"),
            ],
        ),
        TaskSpec(
            "agentic",
            "Agentic",
            "python_ai",
            "Bounded agent with independent evaluation.",
            "◎",
            "agentic_task",
            {
                "agent": "quality-local",
                "specification": {
                    "schema_version": 1,
                    "objective": "Produce a complete, validated observation table",
                    "output_schema": {"type": "object", "required": ["observations"]},
                },
                "inputs": {},
                "requirements": [
                    {
                        "id": "completeness",
                        "description": "All expected records are present",
                        "evaluator": "record_coverage",
                        "expected": {"count": 4},
                        "weight": 1.0,
                        "mandatory": True,
                        "minimum_score": 1.0,
                    }
                ],
                "evaluation": {"aggregation": "weighted_mean", "threshold": 1.0},
                "outputs": {"result": "outputs/result.json", "evaluation": "outputs/evaluation.json"},
            },
            [
                _f("agent", "Agent profile", help="Name of an <code>[agent.&lt;name&gt;]</code> section in <code>dagon.ini</code>."),
                _f("specification", "Specification (JSON)", "json"),
                _f("inputs", "Inputs (JSON)", "json", help="name → <code>workflow:///task/path</code>."),
                _f("outputs", "Outputs (JSON)", "json"),
                _f("requirements", "Requirements (JSON list)", "json"),
                _f("evaluation", "Evaluation (JSON)", "json"),
                _f("tools", "Tools", "list", advanced=True, help="Leave empty to use the profile tools."),
                _f("guardrails", "Guardrails", "list", advanced=True),
                _f("budgets", "Budgets (JSON)", "json", advanced=True),
                PRE_STAGE_FIELD,
            ],
        ),
    ]
}

TASK_TYPE_LABELS: dict[str, str] = {task_type: spec.label for task_type, spec in TASK_SPECS.items()}


def palette() -> list[dict[str, Any]]:
    """Group the task specs for the editor palette."""
    groups = []
    for key, label in CATEGORIES:
        specs = [spec.public() for spec in TASK_SPECS.values() if spec.category == key]
        groups.append({"key": key, "label": label, "types": specs})
    return groups


# ---------------------------------------------------------------------------
# Value helpers
# ---------------------------------------------------------------------------


def _present(value: Any) -> bool:
    return value not in (None, "", {}, [])


def _as_list(value: Any) -> list[str] | None:
    if value is None or value == "":
        return None
    if isinstance(value, str):
        return [item.strip() for item in re.split(r"[\n,]+", value) if item.strip()]
    if isinstance(value, list):
        return [str(item) for item in value]
    return None


def _as_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def normalize_command_references(command: str) -> str:
    """Separate ``workflow://`` references from glued shell punctuation.

    DAGonStar scans a reference until the next space, so ``cat workflow:///a/x;``
    would otherwise ask DAGonStar to stage ``x;``.
    """
    if not command:
        return ""
    pieces: list[str] = []
    position = 0
    for match in WORKFLOW_REFERENCE.finditer(command):
        end = match.end()
        pieces.append(command[position:end])
        if end < len(command) and command[end] in _REFERENCE_TERMINATORS:
            pieces.append(" ")
        position = end
    pieces.append(command[position:])
    return "".join(pieces)


def _input_assignments(inputs: Any) -> str:
    """Render named inputs as shell assignments that DAGonStar can rewrite."""
    if not isinstance(inputs, dict):
        return ""
    lines = []
    for name, value in inputs.items():
        if not isinstance(name, str) or not SHELL_IDENTIFIER.match(name):
            continue
        text = "" if value is None else str(value)
        if WORKFLOW_REFERENCE.fullmatch(text):
            # Unquoted and followed by a space: DAGonStar replaces the token
            # with the staged path inside the task working directory.
            lines.append(f"{name}={text} ;")
        else:
            lines.append(f"{name}={shlex.quote(text)} ;")
    return "\n".join(lines)


def runtime_command(task_type: str, config: dict[str, Any]) -> str:
    """Return the command string handed to DAGonStar for command task types."""
    command = str(config.get("command") or "")
    inputs = config.get("inputs") or {}
    if task_type == "checkpoint":
        references = [str(value) for value in inputs.values()] if isinstance(inputs, dict) else []
        return normalize_command_references(" ".join(part for part in [*references, command.strip()] if part))
    assignments = _input_assignments(inputs)
    body = f"{assignments}\n{command}" if assignments else command
    return normalize_command_references(body)


def _dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _llm_prompt(config: dict[str, Any]) -> dict[str, Any] | str:
    prompt = config.get("prompt", {})
    if isinstance(prompt, str):
        try:
            parsed = json.loads(prompt)
        except json.JSONDecodeError:
            return {"messages": [{"role": "user", "content": prompt}]}
        return parsed
    return prompt if isinstance(prompt, dict) else {}


# ---------------------------------------------------------------------------
# DAGonStar task construction
# ---------------------------------------------------------------------------


def dagon_task_arguments(task_type: str, name: str, config: dict[str, Any]) -> tuple[list[Any], dict[str, Any]]:
    """Return ``(args, kwargs)`` for ``DagonTask(TaskType.X, *args, **kwargs)``.

    ``args`` excludes the task type itself.  Empty optional values are left
    out so DAGonStar keeps its own defaults.
    """
    config = config or {}
    if task_type in COMMAND_TYPES:
        args: list[Any] = [name, runtime_command(task_type, config)]
        kwargs: dict[str, Any] = {}
        if task_type == "slurm":
            for key in ("partition", "nodelist", "comment"):
                if _present(config.get(key)):
                    kwargs[key] = str(config[key])
            time = config.get("time") or config.get("time_limit")
            if _present(time):
                kwargs["time"] = str(time)
            for key in ("nodes", "ntasks", "ntasks_per_node", "memory"):
                value = _as_int(config.get(key))
                if value is not None:
                    kwargs[key] = value
            options = _as_list(config.get("sbatch_options"))
            if options:
                kwargs["sbatch_options"] = options
            if isinstance(config.get("pre_stage"), bool):
                kwargs["pre_stage"] = config["pre_stage"]
        elif task_type == "docker":
            for key in ("image", "volume"):
                if _present(config.get(key)):
                    kwargs[key] = str(config[key])
            for key in ("remove", "pull"):
                if isinstance(config.get(key), bool):
                    kwargs[key] = config[key]
            devices = _as_list(config.get("devices"))
            if devices:
                kwargs["devices"] = devices
        elif task_type == "kubernetes":
            for key in ("image", "namespace"):
                if _present(config.get(key)):
                    kwargs[key] = str(config[key])
            for key in ("remove", "privileged"):
                if isinstance(config.get(key), bool):
                    kwargs[key] = config[key]
            timeout = _as_int(config.get("cleanup_timeout"))
            if timeout is not None:
                kwargs["cleanup_timeout"] = timeout
            if _present(config.get("volumes")):
                kwargs["volumes"] = config["volumes"]
            devices = _as_list(config.get("devices"))
            if devices:
                kwargs["devices"] = devices
        elif task_type == "apptainer":
            if _present(config.get("image")):
                kwargs["image"] = str(config["image"])
            if _present(config.get("overlay_size")):
                kwargs["overlay_size"] = str(config["overlay_size"])
            if isinstance(config.get("remove"), bool):
                kwargs["remove"] = config["remove"]
            bind_paths = _as_list(config.get("bind_paths"))
            if bind_paths:
                kwargs["bind_paths"] = bind_paths
        elif task_type == "nomad":
            for key in ("image", "nomad_address", "datacenter", "region", "network_mode", "volume"):
                if _present(config.get(key)):
                    kwargs[key] = str(config[key])
            for key in ("cpu", "memory"):
                value = _as_int(config.get(key))
                if value is not None:
                    kwargs[key] = value
        elif task_type == "cloud":
            kwargs["provider"] = config.get("provider") or ""
            kwargs["ssh_username"] = config.get("ssh_username") or ""
            kwargs["key_options"] = {"key_path": "", **_dict(config.get("key_options"))}
            for key in ("instance_id", "instance_name", "endpoint"):
                if _present(config.get(key)):
                    kwargs[key] = str(config[key])
            if _present(config.get("instance_flavour")):
                kwargs["instance_flavour"] = config["instance_flavour"]
            if isinstance(config.get("stop_instance"), bool):
                kwargs["stop_instance"] = config["stop_instance"]
        return args, kwargs

    if task_type == "native":
        kwargs = {"inputs": _dict(config.get("inputs")), "outputs": _dict(config.get("outputs"))}
        kwargs["executor"] = config.get("executor") or "local"
        for key in ("resources", "environment"):
            if _present(config.get(key)):
                kwargs[key] = _dict(config.get(key))
        if _present(config.get("python")):
            kwargs["python"] = str(config["python"])
        return [name, str(config.get("callable") or "")], kwargs

    if task_type == "web":
        return [name, web_specification(config)], {}

    if task_type == "llm":
        kwargs = {
            "provider": config.get("provider") or "",
            "params": _dict(config.get("params")),
            "input_files": _dict(config.get("input_files", config.get("inputs"))),
            "output_file": config.get("output_file") or "response.json",
        }
        timeout = _as_int(config.get("timeout"))
        if timeout is not None:
            kwargs["timeout"] = timeout
        return [name, _llm_prompt(config)], kwargs

    if task_type == "faas":
        return [name, faas_specification(config)], {}

    if task_type == "iot":
        return [name], iot_specification(config)

    if task_type == "agentic":
        kwargs = {"name": name, **agentic_contract(config)}
        if isinstance(config.get("pre_stage"), bool):
            kwargs["pre_stage"] = config["pre_stage"]
        return [], kwargs

    raise ValueError(f"Unsupported task type {task_type}")


def web_specification(config: dict[str, Any]) -> dict[str, Any]:
    # ``inputs`` is DAGonWeb's editor-only mapping; DAGonStar scans the
    # specification for references on its own.
    return {key: value for key, value in (config or {}).items() if key != "inputs"}


FAAS_KEYS = ("provider", "profile", "function", "inputs", "outputs", "invocation", "timeout", "retry", "completion", "input_policy", "output_policy", "provider_options", "annotations")


def faas_specification(config: dict[str, Any]) -> dict[str, Any]:
    spec: dict[str, Any] = {}
    for key in FAAS_KEYS:
        value = config.get(key)
        if key == "timeout":
            value = _as_int(value)
        if value is not None and value != "":
            spec[key] = value
    return spec


IOT_KEYS = ("operation", "provider", "endpoint_ref", "target", "resource", "action", "request", "inputs", "outputs", "completion", "delivery", "placement", "runtime", "artifact", "timeout", "credential_ref", "provider_options")
IOT_ALIASES = {"endpointRef": "endpoint_ref", "credentialRef": "credential_ref", "providerOptions": "provider_options"}


def iot_specification(config: dict[str, Any]) -> dict[str, Any]:
    spec: dict[str, Any] = {}
    for key in IOT_KEYS:
        value = config.get(key)
        if key == "timeout" and value not in (None, ""):
            try:
                value = float(value)
            except (TypeError, ValueError):
                value = None
        if _present(value):
            spec[key] = value
    spec.setdefault("operation", "observe")
    return spec


AGENTIC_KEYS = ("agent", "specification", "requirements", "evaluation", "inputs", "outputs", "tools", "guardrails", "budgets")


def agentic_contract(config: dict[str, Any]) -> dict[str, Any]:
    contract: dict[str, Any] = {}
    for key in AGENTIC_KEYS:
        value = config.get(key)
        if key in ("tools", "guardrails"):
            if isinstance(value, list):
                contract[key] = [str(item) for item in value]
            elif isinstance(value, str) and value.strip():
                contract[key] = _as_list(value)
            continue
        if value is not None and value != "":
            contract[key] = value
    return contract


# ---------------------------------------------------------------------------
# DAGonStar JSON document fields
# ---------------------------------------------------------------------------


def document_fields(task_type: str, config: dict[str, Any]) -> dict[str, Any]:
    """Return the task-level fields DAGonStar's JSON loader reads for a type."""
    config = config or {}
    if task_type in COMMAND_TYPES:
        fields: dict[str, Any] = {"command": runtime_command(task_type, config)}
        if task_type == "slurm" and isinstance(config.get("pre_stage"), bool):
            fields["pre_stage"] = config["pre_stage"]
        return fields
    if task_type == "native":
        _, kwargs = dagon_task_arguments(task_type, "native", config)
        return {"command": "", "function": config.get("callable", ""), **kwargs}
    if task_type == "web":
        return {"command": "", "specification": web_specification(config)}
    if task_type == "llm":
        prompt = _llm_prompt(config)
        _, kwargs = dagon_task_arguments(task_type, "llm", config)
        return {"command": json.dumps(prompt) if isinstance(prompt, dict) else prompt, **kwargs}
    if task_type == "faas":
        return faas_specification(config)
    if task_type == "iot":
        spec = iot_specification(config)
        return {"command": None, "iot": {"schemaVersion": 1, **spec}}
    if task_type == "agentic":
        return {"command": None, "agentic": agentic_contract(config)}
    return {"command": str(config.get("command") or "")}


def config_from_document(task_data: dict[str, Any]) -> dict[str, Any]:
    """Derive an editor configuration from a plain DAGonStar task entry."""
    task_type = str(task_data.get("type", "batch")).lower()
    if task_type in COMMAND_TYPES:
        config: dict[str, Any] = {"command": task_data.get("command") or "", "inputs": {}}
        if task_type == "checkpoint":
            command = config["command"]
            references = [match.group(0) for match in WORKFLOW_REFERENCE.finditer(command)]
            remainder = WORKFLOW_REFERENCE.sub("", command).strip()
            config = {"inputs": {f"ref_{index}": reference for index, reference in enumerate(references, start=1)}, "command": remainder}
        for key in ("pre_stage", "image", "partition", "time", "namespace"):
            if key in task_data and task_data[key] is not None:
                config[key] = task_data[key]
        return config
    if task_type == "native":
        config = {"callable": task_data.get("function", ""), "source": "", "requirements": ""}
        for key in ("inputs", "outputs", "resources", "environment"):
            config[key] = _dict(task_data.get(key))
        config["executor"] = task_data.get("executor") or "local"
        config["python"] = task_data.get("python") or ""
        return config
    if task_type == "web":
        return _dict(task_data.get("specification"))
    if task_type == "llm":
        prompt: Any = task_data.get("command") or "{}"
        try:
            prompt = json.loads(prompt) if isinstance(prompt, str) else prompt
        except json.JSONDecodeError:
            prompt = {"messages": [{"role": "user", "content": prompt}]}
        return {
            "provider": task_data.get("provider") or "",
            "prompt": prompt,
            "params": _dict(task_data.get("params")),
            "input_files": _dict(task_data.get("input_files")),
            "output_file": task_data.get("output_file") or "response.json",
            "timeout": task_data.get("timeout", 120),
        }
    if task_type == "faas":
        return {key: task_data[key] for key in FAAS_KEYS if key in task_data}
    if task_type == "iot":
        raw = _dict(task_data.get("iot"))
        return {IOT_ALIASES.get(key, key): value for key, value in raw.items() if key != "schemaVersion"}
    if task_type == "agentic":
        raw = _dict(task_data.get("agentic"))
        raw.pop("schema_version", None)
        return raw
    return {"command": task_data.get("command") or ""}


def editor_references(task_type: str, config: dict[str, Any]) -> dict[str, Any]:
    """Return the configuration values scanned for ``workflow://`` links."""
    config = dict(config or {})
    if task_type == "native":
        config.pop("source", None)
    config.pop("explicit_prevs", None)
    return config
