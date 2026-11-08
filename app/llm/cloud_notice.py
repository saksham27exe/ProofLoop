"""The notice an administrator must accept before ProofLoop may use a cloud model.

The server shows this exact text (``GET /settings``) and records which version was accepted,
by whom and when. Bump ``CLOUD_NOTICE_VERSION`` whenever the wording changes materially.
"""

from __future__ import annotations

CLOUD_NOTICE_VERSION = "2026-11-08.1"

CLOUD_NOTICE = """\
By switching ProofLoop to a cloud model you accept the following:

1. Your data leaves this machine. Task instructions, the structure of connected data sources (table and column \
names, allowed values) and the actual rows and file contents the AI agent reads are sent over the internet to the \
cloud provider (by default NVIDIA NIM, integrate.api.nvidia.com). ProofLoop's offline and privacy guarantees no \
longer apply to that data.
2. The provider's terms apply, not ProofLoop's. The provider may log, store, review or otherwise process what it \
receives. Free tiers may be rate-limited, changed or withdrawn at any time.
3. Do not use it with personal, confidential, regulated or production data unless you are authorised to send that \
data to the provider.
4. The AI agent can change your data. A cloud model is more capable, but it can still misunderstand a task, change \
the wrong records or delete data. ProofLoop takes a backup before each run and verifies the outcome, but it cannot \
guarantee that every mistake is prevented or reversible.
5. You are responsible. You, and the organisation you act for, are solely responsible for the decision to use a \
cloud model, for the data you send to it and for any loss, damage, breach or other consequence that results. \
ProofLoop is provided "as is", without warranty of any kind (see LICENSE), and its authors accept no liability.

Verdicts are not affected: VERIFIED / FAILED / INCONCLUSIVE are still decided locally by deterministic rules, never \
by the cloud model.
"""
