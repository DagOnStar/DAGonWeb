# Security

DAGonWeb is a demo-ready project skeleton, not a hardened multi-tenant execution sandbox.

Important safeguards included:

- Authentication with Flask-Login.
- CSRF protection for forms and API save operations.
- Role-based permissions (`workflows.create`, `workflows.run`, `workflows.publish`, `assistant.use`, `admin.access`); the last active administrator cannot be removed.
- Single sign-on uses the OpenID Connect authorization-code flow with `state`, `nonce` and PKCE, validates issuer, audience, nonce and expiry, and links an existing account only for a verified email. Post-login redirects accept only local paths.
- Secrets in the settings (client secrets, SMTP password, LLM API key) are never rendered back into pages, and can be supplied through environment variables instead of the database.
- Uploaded logos are checked by signature, SVGs with scripts are rejected, and the logo is served with a sandboxing Content-Security-Policy.
- Python and ZIP imports are parsed with `ast` and never executed; ZIP entries are checked for path traversal, size and count. Exported bundles strip secret-looking `dagon.ini` values.
- Interactive uploads are size-limited, file names are sanitised, and files are written only into the run's directory under the scratch root.
- An audit log records sign-ins (including failures), administration changes, imports and runs; it has its own retention period.
- Guest dry runs never execute commands, never import Native task code, and never write to the database or the scratch directory; documents are limited to 512 KB and 60 tasks.
- Responses carry `X-Content-Type-Options: nosniff`, `Referrer-Policy: strict-origin-when-cross-origin`, `X-Frame-Options: SAMEORIGIN` and a restrictive `Permissions-Policy`. Cookies are HttpOnly and SameSite=Lax; set `SESSION_COOKIE_SECURE=true` behind HTTPS. “Remember me” lasts 30 days.
- Account erasure removes workflows, runs and scratch files, and detaches audit entries (no user link, email or IP). See [privacy_gdpr.md](privacy_gdpr.md).
- Ownership checks for workflow editing.
- Task-run scratch browsing, preview, file downloads, and ZIP archives are restricted to the authorized task scratch root. File names are escaped before they are rendered.
- Every run, its task directories, its checkpoint, and CWL exports live under the configured scratch root. The executor always overrides `[batch] scratch_dir_base` and `remove_dir`, even when a per-workflow `dagon.ini` sets them.
- Workflows with active runs cannot be deleted. Deleting a run or a workflow removes its scratch directory.
- The service worker caches only `GET` responses, and only for offline fallback. Authenticated mutations always go to the network.

Production hardening recommendations:

- Run untrusted tasks in isolated containers or batch jobs.
- Disable arbitrary Bash/Python execution for untrusted users. Native tasks install their `requirements` into a per-run virtual environment, and Agentic and LLM tasks call the providers configured in `dagon.ini`.
- Add resource limits and quotas; run workflows on dedicated Redis workers rather than in the web process.
- The LLM assistant sends workflow documents to the configured endpoint; use a self-hosted model for sensitive workflows.
- Use HTTPS and a production database.
- Use secret management instead of plaintext `.env` files.
