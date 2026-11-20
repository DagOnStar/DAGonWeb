"""Site checks for HPC cluster mode (``flask --app wsgi hpc-check``).

Run on the real login node after setting up the runner: each check says what
works and what to fix. With ``--email`` the checks sign in as that user's
linked account; with ``--run`` they also run a tiny workflow end to end, as
that user, and delete it again.
"""
from __future__ import annotations

import base64
import socket
from dataclasses import dataclass
from typing import Any

from . import hpc, settings, sshsig
from .extensions import db
from .models import User, Workflow, WorkflowRun

EXPECTED_RUNNER_VERSION = "3"


@dataclass
class Check:
    label: str
    ok: bool | None  # None: a warning, not a failure
    detail: str = ""

    @property
    def mark(self) -> str:
        return {True: "ok", False: "FAIL", None: "warn"}[self.ok]


def probe_host_key(host: str, port: int, pinned: str, timeout: float = 10) -> tuple[bool, str]:
    """Connect without authenticating and compare the host key with the pinned one."""
    import paramiko

    key_type, key_data = hpc.parse_host_key(pinned)
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
    except OSError as exc:
        return False, f"cannot connect to {host}:{port}: {exc}"
    transport = paramiko.Transport(sock)
    try:
        transport.get_security_options().key_types = [key_type]
        transport.start_client(timeout=timeout)
        offered = transport.get_remote_server_key()
        if offered.get_name() != key_type or offered.asbytes() != base64.b64decode(key_data):
            return False, "the login node presents a different host key than the pinned one"
        return True, f"{key_type} host key matches"
    except Exception as exc:  # noqa: BLE001 - paramiko raises several types
        return False, f"SSH handshake failed: {exc}"
    finally:
        transport.close()


def local_dagonstar() -> str:
    try:
        from importlib.metadata import version

        return version("dagonstar")
    except Exception:  # noqa: BLE001
        return ""


def site_checks() -> list[Check]:
    checks = [Check("HPC cluster mode is on", bool(settings.get("hpc_enabled")), "" if settings.get("hpc_enabled") else "set hpc_enabled in Administration → HPC cluster")]
    pinned = str(settings.get("hpc_ssh_host_key") or "")
    host, port = str(settings.get("hpc_ssh_host")), int(settings.get("hpc_ssh_port"))
    if not pinned:
        checks.append(Check("Host key is pinned", False, "paste the login node's public host key into hpc_ssh_host_key"))
        return checks
    try:
        hpc.parse_host_key(pinned)
    except hpc.HpcError as exc:
        checks.append(Check("Host key is pinned", False, str(exc)))
        return checks
    ok, detail = probe_host_key(host, port, pinned)
    checks.append(Check(f"Login node {host}:{port} is reachable with the pinned host key", ok, detail))
    checks.append(Check("Private keys are encrypted with HPC_KEY_SECRET", None if not _has_key_secret() else True,
                        "" if _has_key_secret() else "HPC_KEY_SECRET is not set: keys are encrypted with a key derived from SECRET_KEY"))
    return checks


def _has_key_secret() -> bool:
    from flask import current_app

    return bool(current_app.config.get("HPC_KEY_SECRET"))


def account_checks(user: User) -> list[Check]:
    if not user.unix_linked:
        return [Check(f"{user.email} has a verified cluster account", False, "link and verify it on the account page first")]
    checks = [Check(f"{user.email} has a verified cluster account", True, user.unix_username or "")]
    try:
        with hpc.client_for(user) as client:
            info = client.ping()
            checks.append(Check(f"Runner answers as {info.get('user')} (uid {info.get('uid')})", True, str(info.get("base"))))
            version = str(info.get("version") or "")
            checks.append(Check(f"Runner version {version or 'unknown'}", version == EXPECTED_RUNNER_VERSION,
                                "" if version == EXPECTED_RUNNER_VERSION else f"install hpc/dagonweb_runner.py from this release (version {EXPECTED_RUNNER_VERSION})"))
            remote, local = str(info.get("dagonstar") or ""), local_dagonstar()
            checks.append(Check(f"DAGonStar on the cluster: {remote or 'not found'}", (remote == local) if remote else False,
                                "" if remote == local else f"DAGonWeb uses {local or 'unknown'}; install the same version in the runner's Python"))
            details = client.info()
            queue = details.get("queue")
            checks.append(Check("Slurm (squeue) is available to the user", None if queue is None else not details.get("queue_error"),
                                "Slurm tasks and the slurm orchestrator need sbatch/squeue on the login node" if queue is None else str(details.get("queue_error") or f"{len(queue)} job(s) queued")))
            filesystem = details.get("filesystem") or {}
            if filesystem.get("free") is not None:
                free_gb = filesystem["free"] / 1024**3
                checks.append(Check(f"Free space in the runner base: {free_gb:.1f} GB", free_gb >= 1 or None))
            keys = client.keys()
            own = sshsig.key_blob(user.unix_public_key or "")
            found = any(sshsig.key_blob(str(entry.get("key") or "")) == own for entry in keys)
            checks.append(Check(f"The runner reads the user's authorized_keys ({len(keys)} key(s))", found,
                                "" if found else "DAGonWeb's own key is not in the files the runner reads: set authorized_keys_files in runner.json to match sshd's AuthorizedKeysFile"))
            if settings.get("hpc_ssh_login_enabled"):
                full = [entry for entry in keys if sshsig.usable_authorized_key(str(entry.get("options") or ""))]
                checks.append(Check("The user has a key for SSH-key sign-in", bool(full) or None,
                                    "" if full else "no key without command= or from=: this user cannot sign in with an SSH key"))
    except hpc.HpcError as exc:
        checks.append(Check("Runner reachable as the user", False, str(exc)))
    return checks


def run_check(user: User, keep: bool = False) -> Check:
    """Run the Batch template as ``user`` on the cluster, then remove it (unless ``keep``)."""
    from .executor.dispatch import run_job
    from .workflows.routes import apply_workflow_template

    workflow = Workflow(owner_id=user.id, name="hpc-check", description="Created by flask hpc-check.")
    db.session.add(workflow)
    db.session.flush()
    apply_workflow_template(workflow, "batch")
    run = WorkflowRun(workflow_id=workflow.id, user_id=user.id, status="pending", scratch_path="", log="", unix_username=user.unix_username)
    db.session.add(run)
    db.session.commit()
    workflow_id, run_id = workflow.id, run.id
    run_job(workflow_id, run_id)
    db.session.expire_all()
    run = db.session.get(WorkflowRun, run_id)
    ok = run.status == "success"
    detail = f"run #{run_id} {run.status}" + ("" if ok else f": {(run.log or '').strip().splitlines()[-1:] or ''}")
    if not keep:
        try:
            with hpc.client_for(user) as client:
                client.delete(run_id)
        except hpc.HpcError as exc:
            detail += f" (run folder not deleted: {exc})"
        db.session.delete(db.session.get(Workflow, workflow_id))
        db.session.commit()
    return Check("A test workflow runs on the cluster as the user", ok, detail)


def all_checks(email: str | None = None, run: bool = False, keep: bool = False) -> list[Check]:
    checks = site_checks()
    if email:
        user = User.query.filter_by(email=email.lower()).first()
        if user is None:
            return checks + [Check(f"User {email} exists", False)]
        account = account_checks(user)
        checks += account
        if run and all(check.ok is not False for check in account):
            checks.append(run_check(user, keep))
    return checks


def summary(checks: list[Check]) -> dict[str, Any]:
    return {"failed": sum(check.ok is False for check in checks), "warnings": sum(check.ok is None for check in checks)}
