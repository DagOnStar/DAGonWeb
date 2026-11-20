from __future__ import annotations

import json
import secrets
import hashlib
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from werkzeug.security import check_password_hash, generate_password_hash
from flask_login import UserMixin
from .extensions import db
from .task_specs import TASK_TYPE_LABELS, document_fields, runtime_type

user_roles = db.Table(
    "user_roles",
    db.Column("user_id", db.Integer, db.ForeignKey("users.id"), primary_key=True),
    db.Column("role_id", db.Integer, db.ForeignKey("roles.id"), primary_key=True),
)

class TaskType(str, Enum):
    """DAGonStar 3.0 task types supported by the editor and executor."""

    CHECKPOINT = "checkpoint"
    BATCH = "batch"
    SLURM = "slurm"
    CLOUD = "cloud"
    DOCKER = "docker"
    KUBERNETES = "kubernetes"
    APPTAINER = "apptainer"
    NOMAD = "nomad"
    LLM = "llm"
    NATIVE = "native"
    WEB = "web"
    FAAS = "faas"
    IOT = "iot"
    AGENTIC = "agentic"
    META = "meta"
    VOLUME = "volume"
    # DAGonWeb interactive types; DAGonStar executes them as BATCH tasks.
    UPLOAD = "upload"
    FORM = "form"
    TABLE = "table"
    CHART = "chart"
    DOWNLOAD = "download"


LEGACY_TASK_TYPES = {"bash": TaskType.BATCH.value, "python": TaskType.BATCH.value}

class RunStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"


ACTIVE_RUN_STATUSES = {RunStatus.PENDING.value, RunStatus.RUNNING.value}
LINK_DATA = "data"
LINK_EXPLICIT = "explicit"

class TimestampMixin:
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc), nullable=False)

# Fine-grained permissions granted through roles. ``admin`` implies all.
PERMISSIONS: dict[str, str] = {
    "workflows.create": "Create, import and edit own workflows",
    "workflows.run": "Run workflows and manage own runs",
    "workflows.publish": "Make own workflows public",
    "assistant.use": "Use the LLM workflow assistant",
    "admin.access": "Open the administration panel",
}
DEFAULT_USER_PERMISSIONS = ["workflows.create", "workflows.run", "workflows.publish", "assistant.use"]


class Role(TimestampMixin, db.Model):
    __tablename__ = "roles"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), unique=True, nullable=False)
    description = db.Column(db.String(255), default="")
    permissions_json = db.Column(db.Text, default="[]", nullable=False, server_default="[]")

    @property
    def permissions(self) -> list[str]:
        try:
            value = json.loads(self.permissions_json or "[]")
        except json.JSONDecodeError:
            return []
        return [item for item in value if item in PERMISSIONS] if isinstance(value, list) else []

    @permissions.setter
    def permissions(self, value: list[str]) -> None:
        self.permissions_json = json.dumps(sorted({item for item in value if item in PERMISSIONS}))

class User(UserMixin, TimestampMixin, db.Model):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    name = db.Column(db.String(255), nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    active = db.Column(db.Boolean, default=True, nullable=False)
    # ``local`` accounts sign in with a password; SSO accounts store the
    # provider key and the provider's stable subject identifier.
    auth_provider = db.Column(db.String(40), default="local", nullable=False, server_default="local")
    external_id = db.Column(db.String(255), index=True)
    last_login_at = db.Column(db.DateTime)
    # GDPR: the privacy notice version the user accepted, and when.
    privacy_version = db.Column(db.String(40))
    privacy_accepted_at = db.Column(db.DateTime)
    # HPC cluster account (``app/hpc.py``). ``unix_username`` is only a claim
    # until ``unix_linked_at`` is set: DAGonWeb proved it can sign in as that
    # Unix user with the key pair below, whose public half the user added to
    # ``~/.ssh/authorized_keys``. The private key is encrypted at rest.
    unix_username = db.Column(db.String(64), index=True)
    unix_public_key = db.Column(db.Text)
    unix_private_key = db.Column(db.Text)
    unix_linked_at = db.Column(db.DateTime)
    roles = db.relationship("Role", secondary=user_roles, lazy="joined", backref=db.backref("users", lazy="dynamic"))

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)

    def set_unusable_password(self) -> None:
        """SSO accounts get a random password nobody knows."""
        self.password_hash = generate_password_hash(secrets.token_urlsafe(48))

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)

    def has_role(self, role_name: str) -> bool:
        return any(role.name == role_name for role in self.roles)

    @property
    def is_admin(self) -> bool:
        return self.has_role("admin")

    def can(self, permission: str) -> bool:
        """Return whether any of the user's roles grants ``permission``."""
        return self.is_admin or any(permission in role.permissions for role in self.roles)

    @property
    def is_active(self) -> bool:  # type: ignore[override]
        return self.active

    @property
    def unix_linked(self) -> bool:
        return bool(self.unix_username and self.unix_linked_at and self.unix_private_key)


class RegistrationToken(TimestampMixin, db.Model):
    __tablename__ = "registration_tokens"
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), nullable=False, index=True)
    token_hash = db.Column(db.String(64), unique=True, nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)
    used_at = db.Column(db.DateTime)

    @staticmethod
    def hash_token(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    @classmethod
    def issue(cls, email: str, expires_at: datetime) -> tuple["RegistrationToken", str]:
        token = secrets.token_urlsafe(32)
        record = cls(email=email.lower(), token_hash=cls.hash_token(token), expires_at=expires_at)
        return record, token

class AuditLog(db.Model):
    """Append-only record of security- and operations-relevant events."""

    __tablename__ = "audit_logs"
    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), index=True)
    action = db.Column(db.String(80), nullable=False, index=True)
    target = db.Column(db.String(255), default="", nullable=False)
    detail = db.Column(db.Text, default="", nullable=False)
    ip_address = db.Column(db.String(64), default="", nullable=False)
    user = db.relationship("User")


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
    dagon_ini_json = db.Column(db.Text, default="{}")
    dagonweb_json = db.Column(db.Text, default="{}")
    owner = db.relationship("User", backref=db.backref("workflows", lazy="dynamic"))
    tasks = db.relationship("WorkflowTask", cascade="all, delete-orphan", backref="workflow", lazy="joined")
    links = db.relationship("WorkflowLink", cascade="all, delete-orphan", backref="workflow", lazy="joined")

    @property
    def dagon_ini(self) -> dict[str, Any]:
        return json.loads(self.dagon_ini_json or "{}")

    @dagon_ini.setter
    def dagon_ini(self, value: dict[str, Any]) -> None:
        self.dagon_ini_json = json.dumps(value, indent=2, sort_keys=True)

    @property
    def dagonweb(self) -> dict[str, Any]:
        return json.loads(self.dagonweb_json or "{}")

    @dagonweb.setter
    def dagonweb(self, value: dict[str, Any]) -> None:
        self.dagonweb_json = json.dumps(value, indent=2, sort_keys=True)

    @property
    def fair_profile(self) -> dict[str, Any]:
        profile = self.dagonweb.get("fair", {})
        return profile if isinstance(profile, dict) else {}

    @fair_profile.setter
    def fair_profile(self, value: dict[str, Any]) -> None:
        metadata = self.dagonweb
        metadata["fair"] = value
        self.dagonweb = metadata

    @property
    def runtime_options(self) -> dict[str, Any]:
        """DAGonStar ``Workflow`` options kept in the additive extension."""
        options = self.dagonweb.get("runtime", {})
        return options if isinstance(options, dict) else {}

    @runtime_options.setter
    def runtime_options(self, value: dict[str, Any]) -> None:
        metadata = self.dagonweb
        metadata["runtime"] = value
        self.dagonweb = metadata

    @property
    def has_active_runs(self) -> bool:
        return self.runs.filter(WorkflowRun.status.in_(ACTIVE_RUN_STATUSES)).count() > 0

    def as_json(self) -> dict[str, Any]:
        """Return the DAGonStar task-map document for this workflow.

        Native DAGonStar fields follow ``dagon.Workflow.as_json()`` from
        DAGonStar 3.0; editor state stays in the additive ``dagonweb`` keys.
        """
        tasks: dict[str, dict[str, Any]] = {}
        for task in self.tasks:
            config = task.config
            entry: dict[str, Any] = {
                "name": task.name,
                "status": "READY",
                "working_dir": None,
                "nexts": [],
                "prevs": [],
                "explicit_prevs": [],
                "command": "",
                "type": runtime_type(task.normalized_task_type),
            }
            entry.update(document_fields(task.normalized_task_type, config, task.name))
            entry["dagonweb"] = {"x": task.x, "y": task.y, "config": config}
            if entry["type"] != task.normalized_task_type:
                # DAGonWeb interactive kinds run as DAGonStar Batch tasks.
                entry["dagonweb"]["task_type"] = task.normalized_task_type
            tasks[task.name] = entry
        for link in self.links:
            if link.source_uid in tasks and link.target_uid in tasks:
                if link.source_uid not in tasks[link.target_uid]["prevs"]:
                    tasks[link.source_uid]["nexts"].append(link.target_uid)
                    tasks[link.target_uid]["prevs"].append(link.source_uid)
                if link.kind == LINK_EXPLICIT:
                    tasks[link.target_uid]["explicit_prevs"].append(link.source_uid)
        document: dict[str, Any] = {"tasks": tasks, "name": self.name, "id": self.id, "host": "localhost"}
        if self.runtime_options.get("pre_stage"):
            document["pre_stage"] = True
        if self.dagonweb:
            document["dagonweb"] = self.dagonweb
        return document

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
    # ``data`` links come from workflow:// references; ``explicit`` links are
    # DAGonStar ``explicit_prevs`` ordering edges without data staging.
    kind = db.Column(db.String(20), default=LINK_DATA, nullable=False, server_default=LINK_DATA)

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "source_uid": self.source_uid, "source_output": self.source_output, "target_uid": self.target_uid, "target_input": self.target_input, "workflow_uri": self.workflow_uri}

class WorkflowRun(TimestampMixin, db.Model):
    __tablename__ = "workflow_runs"
    id = db.Column(db.Integer, primary_key=True)
    workflow_id = db.Column(db.Integer, db.ForeignKey("workflows.id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    status = db.Column(db.String(30), default=RunStatus.PENDING.value, nullable=False)
    label = db.Column(db.String(255), default="", nullable=False)
    scratch_path = db.Column(db.String(1024), nullable=False)
    log = db.Column(db.Text, default="")
    # Set for runs executed on the HPC cluster: the Unix account the run
    # executes as (always the submitter's own), and ``scratch_path`` is then
    # a path on the cluster, reached only through ``dagonweb-runner``.
    unix_username = db.Column(db.String(64))
    # Cluster runs: what DAGonWeb already read from the runner (event offset,
    # inputs provided, fetched results and FAIR exports), and when a process
    # last followed the run, so another process can pick it up after a restart.
    remote_meta_json = db.Column(db.Text)
    monitor_heartbeat = db.Column(db.DateTime)
    workflow = db.relationship("Workflow", backref=db.backref("runs", cascade="all, delete-orphan", lazy="dynamic"))
    user = db.relationship("User")

    @property
    def remote_meta(self) -> dict[str, Any]:
        try:
            value = json.loads(self.remote_meta_json or "{}")
        except json.JSONDecodeError:
            return {}
        return value if isinstance(value, dict) else {}

    @remote_meta.setter
    def remote_meta(self, value: dict[str, Any]) -> None:
        self.remote_meta_json = json.dumps(value)

    def update_remote_meta(self, **values: Any) -> None:
        meta = self.remote_meta
        meta.update(values)
        self.remote_meta = meta

class TaskRun(TimestampMixin, db.Model):
    __tablename__ = "task_runs"
    id = db.Column(db.Integer, primary_key=True)
    workflow_run_id = db.Column(db.Integer, db.ForeignKey("workflow_runs.id"), nullable=False)
    task_uid = db.Column(db.String(80), nullable=False)
    status = db.Column(db.String(30), default=RunStatus.PENDING.value, nullable=False)
    scratch_path = db.Column(db.String(1024), nullable=False)
    log = db.Column(db.Text, default="")
    run = db.relationship("WorkflowRun", backref=db.backref("task_runs", cascade="all, delete-orphan", lazy="joined"))
