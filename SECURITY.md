# Security and privacy

Do not commit deployment credentials, API keys, invitation codes, administrator tokens, runtime databases, logs, TLS private keys, or user-submitted inputs.

The public repository intentionally excludes:

- `.env` files and secret configuration;
- `webapp/npflow_web.sqlite3*`;
- `webapp/certs/`;
- `webapp/logs/`, `webapp/artifacts/`, and generated results;
- server-specific administrator URLs and access credentials;
- personal profile content not required to reproduce the workflow.

Configure secrets through environment variables or a deployment secret manager. Use only synthetic or appropriately licensed inputs when demonstrating the public system.

Security issues should be reported privately to the corresponding author rather than opened as public issues.
