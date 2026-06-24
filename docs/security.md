# Security

DAGonWeb is a demo-ready project skeleton, not a hardened multi-tenant execution sandbox.

Important safeguards included:

- Authentication with Flask-Login.
- CSRF protection for forms and API save operations.
- Role-based admin checks.
- Ownership checks for workflow editing.
- Task-run scratch browsing, preview, file downloads, and ZIP archives restricted to the authorized task scratch root.

Production hardening recommendations:

- Run untrusted tasks in isolated containers or batch jobs.
- Disable arbitrary Bash/Python execution for untrusted users.
- Add resource limits, quotas, and audit logs.
- Use HTTPS and a production database.
- Use secret management instead of plaintext `.env` files.
