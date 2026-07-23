# Security policy

Please do not open a public issue for a vulnerability that could expose credentials, alter order intent, bypass a risk gate, corrupt point-in-time history, or forge audit records. Report it privately to the repository security contact configured by the deploying organization.

This alpha release is intended to bind to `127.0.0.1`. The operator token is a minimal local mutation guard, not a replacement for network authentication, authorization, TLS, CSRF defenses, a secrets manager, or a production reverse proxy. Do not expose the server to an untrusted network.

Never put secrets in TOML configuration, observation payloads, source URLs, audit details, demo fixtures, or browser storage. A production connector should obtain credentials from a managed secret provider and redact all vendor responses before logging.

Supported security fixes target the latest release on the main branch.
