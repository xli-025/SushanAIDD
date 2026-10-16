# NPFlow Web Deployment Status

Last checked: 2026-06-01

## Runtime

- Project: `/data/AIDD`
- Web app: `/data/AIDD/webapp`
- Process: `python server.py`
- HTTP listener: `0.0.0.0:18088`
- HTTPS listener: `0.0.0.0:18443`
- User autostart: installed with `crontab @reboot`
- User watchdog: installed with a once-per-minute crontab health check
- Health checks on the server:
  - `curl http://127.0.0.1:18088/health`
  - `curl -k https://127.0.0.1:18443/health`

## Access

Default user address:

- `http://HOSTNAME_OR_IP:18088`

Optional HTTPS address:

- `https://HOSTNAME_OR_IP:18443`

Server-local access to both ports works. From the Windows client, direct internal-IP access may still time out if the host or upstream firewall blocks non-SSH ports. To make direct client access work, run:

```bash
cd /data/AIDD/webapp
sudo ./open_port_18088.sh
```

The script opens both TCP `18088` and `18443`. If direct access still times out after that, open those ports in the upstream/network ACL.

Current no-sudo client fallback:

```text
http://127.0.0.1:18088
https://127.0.0.1:18443
```

Use `open_npflow_https.bat` from the Windows workspace to start the SSH tunnel and open Edge for the HTTPS tunnel.

## HTTPS

HTTPS is available as an optional internal-IP entry point. It does not require nginx or a domain.

- Certificate: `/data/AIDD/webapp/certs/npflow-ip.crt`
- Key: `/data/AIDD/webapp/certs/npflow-ip.key`
- SAN: deployment-specific host name or IP, `127.0.0.1`, and `localhost`
- Type: self-signed internal-IP certificate

Browsers will warn unless the certificate is trusted. The Windows workspace has:

- `npflow-ip.crt`
- `trust_npflow_cert_current_user.ps1`
- `trust_npflow_cert_current_user.bat`

## Verification Evidence

- Real web task `#3` completed with `exit_code=0`.
- Real task result zip: `/data/AIDD/webapp/artifacts/task_3/npflow_result.zip`
- Docking validation for task `#3`: `PASS`
- gProfiler outage degraded without blocking and is visible in the UI as a warning.
- User isolation was tested: another user could not see or download task `#3`.
- Admin stop/restart was tested: task `#2` was cancelled, restarted as task `#3`, and no old child processes remained.
- Concurrency scheduler was tested with a dry-run root: 4 tasks yielded 3 running on `0-23`, `24-47`, `48-71` and 1 queued.
- Expired artifact cleanup was tested with an expired probe and cleared both DB paths and files.
- Mobile layout was checked at 361px viewport: task list, warning, and download remained visible.
- Vina configs in both docking and reverse design generate `cpu = 24`, not `cpu = 0`.
- User-facing pages do not expose the admin URL. The admin panel is only served from the hidden token path and admin APIs require `X-Admin-Token`.
- Admin result downloads also use `X-Admin-Token`; the admin token is not placed in download query strings.
- Startup and request logging mask the hidden admin token as `[hidden]`, including query strings from invalid requests.
- Admin pages only read the token from `/admin/<token>`; query-string token fallback is disabled.
- Responses include `Referrer-Policy: no-referrer` to avoid leaking the hidden admin path through browser referrers.
- If the web service restarts while a task is running, the scheduler reconciles completed task result directories and creates the missing zip/artifact records.
- A no-sudo watchdog checks `http://127.0.0.1:18088/health` every minute and calls `start_web.sh` if the service is down.
- The latest UI pass adds a designed background, product-style login page, dashboard summary cards, and a separate dark monitoring console for the hidden admin page.

## Operations

```bash
cd /data/AIDD/webapp
./status_web.sh
./start_web.sh
./stop_web.sh
./install_user_autostart.sh
./make_self_signed_cert.sh
sudo ./open_port_18088.sh
```

`sudo ./open_port_18088.sh` is the only step that requires elevated privileges.
