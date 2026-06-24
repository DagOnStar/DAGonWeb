from __future__ import annotations

import json
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from werkzeug.security import check_password_hash, generate_password_hash
from flask_login import UserMixin
from .extensions import db

user_roles = db.Table(
    "user_roles",
    db.Column("user_id", db.Integer, db.ForeignKey("users.id"), primary_key=True),
    db.Column("role_id", db.Integer, db.ForeignKey("roles.id"), primary_key=True),
)

class TaskType(str, Enum):
    CHECKPOINT = "checkpoint"
    BATCH = "batch"
    SLURM = "slurm"
    CLOUD = "cloud"
    DOCKER = "docker"
    LLM = "llm"
    NATIVE = "native"
    WEB = "web"


TASK_TYPE_LABELS = {
    TaskType.CHECKPOINT.value: "Checkpoint",
    TaskType.BATCH.value: "Batch",
    TaskType.SLURM.value: "Slurm",
    TaskType.CLOUD.value: "Cloud",
    TaskType.DOCKER.value: "Docker",
    TaskType.LLM.value: "LLM",
    TaskType.NATIVE.value: "Native",
    TaskType.WEB.value: "Web",
}

LEGACY_TASK_TYPES = {"bash": TaskType.BATCH.value, "python": TaskType.BATCH.value}

class RunStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"

class TimestampMixin:
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc), nullable=False)

class Role(TimestampMixin, db.Model):
    __tablename__ = "roles"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), unique=True, nullable=False)
    description = db.Column(db.String(255), default="")

class User(UserMixin, TimestampMixin, db.Model):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    name = db.Column(db.String(255), nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    active = db.Column(db.Boolean, default=True, nullable=False)
    roles = db.relationship("Role", secondary=user_roles, lazy="joined", backref=db.backref("users", lazy="dynamic"))

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)

    def has_role(self, role_name: str) -> bool:
        return any(role.name == role_name for role in self.roles)

    @property
    def is_active(self) -> bool:  # type: ignore[override]
        return self.active

class Setting(TimestampMixin, db.Model):
    __tablename__ = "settings"
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(120), unique=True, nullable=False)
    value = db.Column(db.Text, nullable=False)

class Workflow(TimestampMixin, db.Model):
    __tablename__ = "workflows"
    id = db.Column(db.Integer, primary_key=True)
    owner_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    name = db.Column(db.String(255), nullable=False)
    description = db.Column(db.Text, default="")
    is_public = db.Column(db.Boolean, default=False, nullable=False)
    owner = db.relationship("User", backref=db.backref("workflows", lazy="dynamic"))
    tasks = db.relationship("WorkflowTask", cascade="all, delete-orphan", backref="workflow", lazy="joined")
    links = db.relationship("WorkflowLink", cascade="all, delete-orphan", backref="workflow", lazy="joined")

    def as_json(self) -> dict[str, Any]:
        tasks: dict[str, dict[str, Any]] = {}
        for task in self.tasks:
            config = task.config
            tasks[task.name] = {
                "name": task.name,
                "status": "READY",
                "working_dir": None,
                "nexts": [],
                "prevs": [],
                "command": config.get("command", ""),
                "type": task.normalized_task_type,
                "dagonweb": {"x": task.x, "y": task.y, "config": config},
            }
            # DAGonStar's JSON loader receives web-task settings through its
            # native ``specification`` field.  Keep the editor settings in the
            # additive DAGonWeb extension too, so exported documents remain
            # portable and editable by DAGonWeb.
            if task.normalized_task_type == "web":
                tasks[task.name]["specification"] = config
            elif task.normalized_task_type == "llm":
                # LLM tasks keep their Chat Completions request in ``command``
                # and their runtime settings at the task level, as expected by
                # DAGonStar's JSON loader.  The full editor configuration stays
                # in the additive dagonweb extension above.
                prompt = config.get("prompt", {})
                tasks[task.name]["command"] = json.dumps(prompt) if isinstance(prompt, dict) else prompt
                tasks[task.name].update({
                    "provider": config.get("provider", ""),
                    "params": config.get("params", {}),
                    "input_files": config.get("input_files", config.get("inputs", {})),
                    "output_file": config.get("output_file", "response.json"),
                    "timeout": config.get("timeout", 120),
                })
            elif task.normalized_task_type == "native":
                # NativeTask's JSON loader expects its bindings at the task
                # level, rather than inside DAGonWeb's editor extension.
                native_settings = {
                    "function": config.get("callable", ""),
                    "inputs": config.get("inputs", {}),
                    "outputs": config.get("outputs", {}),
                    "executor": config.get("executor", "local"),
                }
                native_settings.update({
                    key: config[key]
                    for key in ("resources", "python", "environment")
                    if config.get(key) not in (None, "", {})
                })
                tasks[task.name].update(native_settings)
        for link in self.links:
            if link.source_uid in tasks and link.target_uid in tasks:
                tasks[link.source_uid]["nexts"].append(link.target_uid)
                tasks[link.target_uid]["prevs"].append(link.source_uid)
        return {"tasks": tasks, "name": self.name, "id": self.id, "host": "localhost"}

    def to_graph_json(self) -> dict[str, Any]:
        """Backward-compatible alias for the portable workflow document."""
        return self.as_json()

class WorkflowTask(TimestampMixin, db.Model):
    __tablename__ = "workflow_tasks"
    id = db.Column(db.Integer, primary_key=True)
    workflow_id = db.Column(db.Integer, db.ForeignKey("workflows.id"), nullable=False)
    uid = db.Column(db.String(80), nullable=False)
    label = db.Column(db.String(255), nullable=False)
    task_type = db.Column(db.String(50), nullable=False)
    x = db.Column(db.Integer, default=100)
    y = db.Column(db.Integer, default=100)
    config_json = db.Column(db.Text, default="{}")

    __table_args__ = (db.UniqueConstraint("workflow_id", "uid", name="uq_workflow_task_uid"),)

    @property
    def config(self) -> dict[str, Any]:
        return json.loads(self.config_json or "{}")

    @config.setter
    def config(self, value: dict[str, Any]) -> None:
        self.config_json = json.dumps(value, indent=2, sort_keys=True)

    @property
    def name(self) -> str:
        return self.uid

    @name.setter
    def name(self, value: str) -> None:
        self.uid = value
        self.label = value

    @property
    def normalized_task_type(self) -> str:
        return LEGACY_TASK_TYPES.get(self.task_type, self.task_type)

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "task_type": self.normalized_task_type, "x": self.x, "y": self.y, "config": self.config}

class WorkflowLink(TimestampMixin, db.Model):
    __tablename__ = "workflow_links"
    id = db.Column(db.Integer, primary_key=True)
    workflow_id = db.Column(db.Integer, db.ForeignKey("workflows.id"), nullable=False)
    source_uid = db.Column(db.String(80), nullable=False)
    source_output = db.Column(db.String(80), default="output")
    target_uid = db.Column(db.String(80), nullable=False)
    target_input = db.Column(db.String(80), default="input")
    workflow_uri = db.Column(db.String(255), nullable=False)

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "source_uid": self.source_uid, "source_output": self.source_output, "target_uid": self.target_uid, "target_input": self.target_input, "workflow_uri": self.workflow_uri}

class WorkflowRun(TimestampMixin, db.Model):
    __tablename__ = "workflow_runs"
    id = db.Column(db.Integer, primary_key=True)
    workflow_id = db.Column(db.Integer, db.ForeignKey("workflows.id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    status = db.Column(db.String(30), default=RunStatus.PENDING.value, nullable=False)
    label = db.Column(db.String(255), default="", nullable=False)
    scratch_path = db.Column(db.String(1024), nullable=False)
    log = db.Column(db.Text, default="")
    workflow = db.relationship("Workflow", backref=db.backref("runs", lazy="dynamic"))
    user = db.relationship("User")

class TaskRun(TimestampMixin, db.Model):
    __tablename__ = "task_runs"
    id = db.Column(db.Integer, primary_key=True)
    workflow_run_id = db.Column(db.Integer, db.ForeignKey("workflow_runs.id"), nullable=False)
    task_uid = db.Column(db.String(80), nullable=False)
    status = db.Column(db.String(30), default=RunStatus.PENDING.value, nullable=False)
    scratch_path = db.Column(db.String(1024), nullable=False)
    log = db.Column(db.Text, default="")
    run = db.relationship("WorkflowRun", backref=db.backref("task_runs", cascade="all, delete-orphan", lazy="joined"))
