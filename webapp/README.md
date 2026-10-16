# NPFlow Agent Web

Shared-user address (TLS is required for login and registration):

```bash
https://HOSTNAME_OR_IP:18443
```

The HTTP listener defaults to `127.0.0.1:18088` and is intended for health
checks or a local reverse proxy. Authentication requests over cleartext HTTP
are rejected. Session cookies are `HttpOnly`, `SameSite=Lax`, and `Secure`.

Start:

```bash
cd /data/AIDD/webapp
./start_web.sh
```

Stop and status:

```bash
./status_web.sh
./stop_web.sh
```

Install no-sudo autostart and a once-per-minute watchdog for the `xian` user:

```bash
./install_user_autostart.sh
```

Open the administrator login at `/admin` over HTTPS. Enter the token stored in
`settings.admin_token` inside `npflow_web.sqlite3`; the token is submitted only
in the HTTPS `POST /api/admin/login` body and is exchanged for an eight-hour
`Secure`, `HttpOnly`, `SameSite=Strict` cookie. It is never placed in a URL.
`X-Admin-Token` remains available only for trusted automation clients. The
upgrade rotates the former URL-era token once, so retrieve the current token
locally after upgrading rather than reusing an old link:

```bash
sqlite3 npflow_web.sqlite3 "SELECT value FROM settings WHERE key='admin_token';"
```

To rotate it again without printing the replacement (all administrator cookies
are invalidated immediately):

```bash
sqlite3 npflow_web.sqlite3 \
  "UPDATE settings SET value=lower(hex(randomblob(32))) WHERE key='admin_token';"
```

The same upgrade also rotates `session_secret` once, invalidating user sessions
that may have existed before HTTPS-only authentication. Users must sign in
again after the migration; passwords are unchanged.

To expose the built-in HTTPS listener, open TCP 18443:

```bash
cd /data/AIDD/webapp
sudo ./open_port_18088.sh
```

The firewall script opens only TCP 18443 by default. Browsers will warn because
the bundled certificate is self-signed; distribute and trust the certificate
out of band, or replace it with a certificate issued by the site's CA.

For a TLS-terminating reverse proxy, bind HTTP deliberately and trust only the
proxy's exact address or CIDR:

```bash
export NPFLOW_HTTP_BIND_HOST=127.0.0.1
export NPFLOW_TRUSTED_PROXIES=127.0.0.1/32
```

The proxy must overwrite `X-Forwarded-For` and `X-Forwarded-Proto`; the Web app
honors those headers only from configured trusted proxy networks. Do not expose
port 18088 directly. `NPFLOW_ALLOW_INSECURE_AUTH=1` exists only for isolated
development and must not be enabled on a shared network.

Important: `127.0.0.1` always means the current machine. A plain SSH tunnel to
the HTTP listener does not satisfy the default HTTPS requirement; tunnel the
HTTPS port or terminate TLS locally.

Runtime data:

- `npflow_web.sqlite3`: users, tasks, events, artifacts, trusted task resources, settings
- `logs/`: task stdout/stderr summaries
- `artifacts/`: downloadable result zip files
- `certs/`: self-signed IP certificate for HTTPS

There is no source-code default invite code. Set `NPFLOW_INVITE_CODE` before a
database is initialized, or let first initialization generate a random value in
`settings.invite_code`. Existing custom values are preserved during an upgrade;
the one known historical default is identified by a one-way fingerprint and
rotated once because it was already exposed. Read the current value only from
the administrator page over HTTPS. To rotate it again in a maintenance window
without printing the replacement:

```bash
sqlite3 npflow_web.sqlite3 \
  "UPDATE settings SET value=lower(hex(randomblob(18))) WHERE key='invite_code';"
```

Task resource policy (defaults; effective values are shown in the admin page):

- max concurrency: 4
- CPU slots: `0-23`, `24-47`, `48-71`, `72-95`
- per task: `NPFLOW_TASK_CPU_LIMIT=24`, `OMP_NUM_THREADS=24`, BLAS/MKL/NUMEXPR limited
- launcher: `taskset + nice + ionice`
- min free disk before new task: 5 GB (reject only below 5 GB; the legacy 15 GB setting is migrated on startup)
  Space is calculated using 1024^3 bytes per GB (GiB), without rounding before submission or scheduler checks.
- result retention: 2 days

Completion and cleanup safety:

- Validated form fields bypass Ollama parameter extraction when a mode is
  explicitly selected, or when `auto` has no notes (the existing full-workflow
  default). The original disease still passes through disease-name adaptation
  in the network stage. Unknown Chinese names may still require translation.
- `auto` with supplemental instructions retains model-based mode inference;
  timeouts are reported, never silently converted into a full-workflow request.
  Free-text CLI input also retains model extraction. Input-source markers in
  task logs distinguish structured requests from model-dependent requests.
- A zero process exit code is not sufficient for `completed`. The server checks
  `results/web_tasks/task_<id>/pipeline_status.json`, its task/run identity,
  requested stages, and every declared required artifact.
- A valid run that produces no usable result is recorded as `no_result`.
- Logs are display-only and never grant permission to delete module paths.
- Explicit extra targets and ligands are stored as JSON arrays and passed to the
  orchestrator separately from model-readable task prose.
- Network runs are cleaned only when their producer manifest exactly binds the
  Web task id, server-issued run id, stage, and physical output directory. The
  trusted paths are persisted in `task_resources`; formula source runs are
  tracked independently.
- Every terminal state receives a retention deadline. Failed filesystem cleanup
  retains its database references and is retried instead of being forgotten.
  Failed explicit deletions remain `deleting` only until their scheduled retry;
  the scheduler retries them so they cannot occupy a concurrency slot forever.

Before retaining or archiving logs created by an older version, redact URL
credentials once:

```bash
cd /data/AIDD/webapp
../.venv/bin/python sanitize_access_logs.py webapp.log server.out webapp.out
```

The command replaces each file atomically and reports only the number of changed
lines. Rotate any credential that may already have appeared in a URL; redaction
cannot invalidate a copied secret.
