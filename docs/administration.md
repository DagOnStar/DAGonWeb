# Administration

Administrators open the panel from the navbar (**Admin**). The sidebar has these pages:

| Page | What it does |
| --- | --- |
| **Dashboard** | Run statistics: runs per day over the last 14 days, status counts, success rate, the busiest workflows and users, and the latest failures. |
| **Appearance** | Site name, tagline, footer text, default theme, brand and navbar colors, and the logo. |
| **Authentication** | Local sign-in and self-registration, allowed email domains, and single sign-on with Google, Microsoft Entra ID or any OpenID Connect provider. |
| **Execution** | The run dispatcher (in-process threads or a Redis queue), concurrency, the run timeout, and the back-ends found on the host (Docker, kubectl, sbatch, Apptainer, Nomad). |
| **Storage** | The scratch root, disk usage, run retention and clean-up, and the upload size limit. |
| **SMTP** | Outgoing mail for registration links, with a **Send test email** button. |
| **LLM connector** | The OpenAI-compatible endpoint used by the workflow assistant, with a **Test connection** button. |
| **dagon.ini** | The global DAGonStar configuration (guided form and raw view). |
| **Users** | Create, edit, search, disable and delete accounts; assign roles; unlink an SSO identity. |
| **Roles** | Create roles and choose their permissions. |
| **Audit log** | Sign-ins, failed sign-ins, user/role/settings changes, imports and runs, with filters and CSV export. |

Settings are stored in the `settings` table. Any setting can be pinned from the environment with `DAGONWEB_<KEY>` (see [configuration.md](configuration.md)); a pinned setting is shown read-only in the panel. Secrets (client secrets, SMTP password, LLM API key) are never sent back to the browser: leave the field empty to keep the stored value, or tick **Clear** to remove it.

## Users and roles

Every account has one or more roles, and a role is a list of permissions:

| Permission | Allows |
| --- | --- |
| `workflows.create` | Create, import and copy workflows. |
| `workflows.run` | Start and resume runs. |
| `workflows.publish` | Mark a workflow public, so other signed-in users can read and run it. |
| `assistant.use` | Use the LLM workflow assistant. |
| `admin.access` | Open the administration panel. |

The built-in `admin` role has every permission and cannot be deleted; the built-in `user` role gets `workflows.create`, `workflows.run` and `assistant.use`. Users always manage only their own workflows and runs; administrators can manage everything. DAGonWeb refuses to remove or demote the last active administrator, and refuses to delete a user who still owns workflows or runs (disable the account instead).

## Authentication

- **Local accounts.** Email and password, with self-registration through an emailed link (valid 24 hours). Turn off **Allow email and password sign-in** to make SSO the only option; administrators can always sign in locally, so you cannot lock yourself out.
- **Allowed email domains** limit both self-registration and SSO sign-ups, for example `uniparthenope.it, example.org`.
- **Google** (including Google Workspace for Education, optionally restricted to a hosted domain), **Microsoft Entra ID** (`organizations` accepts any work or school account; use your tenant ID to restrict it) and **generic OpenID Connect** (Keycloak, SATOSA/eduGAIN proxies, Okta, Auth0…).

Register DAGonWeb with the provider using the redirect URI shown on the Authentication page, `https://<host>/login/sso/<google|microsoft|oidc>/callback`. DAGonWeb uses the authorization-code flow with `state`, `nonce` and PKCE, and checks the ID token's issuer, audience, nonce and expiry. A new SSO identity is linked to an existing account only when the provider reports the email as verified; otherwise, with **Create accounts on first SSO sign-in**, a new account with the `user` role is created.

## Logo

Upload PNG, JPEG, WebP, GIF or SVG files up to 512 KB. Files are checked by signature, SVG files containing scripts or event handlers are rejected, and the logo is served from `/branding/logo` with a sandboxing Content-Security-Policy.

## Maintenance

- `flask --app wsgi cleanup-runs [--days N]` deletes finished runs older than the retention period (run it from cron). **Storage → Clean up now** does the same from the panel.
- `flask --app wsgi worker` starts a Redis run worker (see [deployment.md](deployment.md)).
