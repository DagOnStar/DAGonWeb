# Security

DAGonWeb is a demo-ready project skeleton, not a hardened multi-tenant execution sandbox.

Important safeguards included:

- Authentication with Flask-Login.
- CSRF protection for forms and API save operations.
- Role-based admin checks.
- Ownership checks for workflow editing.
- Task-run scratch browsing, preview, file downloads, and ZIP archives are restricted to the authorized task scratch root. File names are escaped before they are rendered.
- Every run, its task directories, its checkpoint, and CWL exports live under the configured scratch root. The executor always overrides `[batch] scratch_dir_base` and `remove_dir`, even when a per-workflow `dagon.ini` sets them.
- Workflows with active runs cannot be deleted. Deleting a run or a workflow removes its scratch directory.
- The service worker caches only `GET` responses, and only for offline fallback. Authenticated mutations always go to the network.

Production hardening recommendations:

- Run untrusted tasks in isolated containers or batch jobs.
- Disable arbitrary Bash/Python execution for untrusted users. Native tasks install their `requirements` into a per-run virtual environment, and Agentic and LLM tasks call the providers configured in `dagon.ini`.
- Add resource limits, quotas, and audit logs.
- Use HTTPS and a production database.
- Use secret management instead of plaintext `.env` files.
