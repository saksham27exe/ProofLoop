# Security policy

## Reporting a vulnerability

Please **do not open a public issue**. Report it privately through
[GitHub security advisories](https://github.com/saksham27exe/ProofLoop/security/advisories/new) with steps to
reproduce and the affected commit. You should get a reply within a week.

In scope, for example: verdict manipulation (an agent or instruction that makes ProofLoop report VERIFIED for a
wrong outcome), trace or evidence tampering that goes undetected, write-policy bypasses through ProofLoop's tools,
authentication or role bypasses, secret leakage (e.g. the cloud API key appearing in traces, logs or API
responses), and path or SQL injection.

## Security model in brief

* ProofLoop is a **local application**: the API and dashboard bind to `127.0.0.1`. It is not hardened for exposure
  to the internet.
* The verifier reads systems through read-only channels; verdicts are computed by deterministic rules.
* The cloud model is off until an administrator accepts the cloud-data notice; the key lives only in `.env`.

Details: [docs/privacy.md](docs/privacy.md) and [docs/configuration.md](docs/configuration.md).
