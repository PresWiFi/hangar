# Launch validation

Observed: 2026-11-01T03:47:46.888417+00:00. Source revision: d299be090bf125b649adc645cddd031cc34f8929.

## Setup and authentication checks

Created an isolated environment with CPython 3.12.16 using `uv venv /tmp/hangar-marketing-onboarding --python /root/.local/bin/python3.12`, then installed requirements.txt and requirements-dev.txt with uv pip.

Command:

```bash
HANGAR_SECURE_COOKIES=false HANGAR_PUBLIC_URL= /tmp/hangar-marketing-onboarding/bin/python -m pytest tests/test_auth.py -q
```

Result: **27 passed**, one upstream AnyIO deprecation warning, 12.20 seconds. Coverage includes initial setup, owner creation, login, password change, session invalidation, configuration migration and password reset. The existing fixtures isolate application data and workspace paths; no production account was changed.

An initial run without those explicit environment overrides produced 4 failures and 23 passes: the host exports HANGAR_SECURE_COOKIES=true, while the TestClient uses HTTP. Secure cookies were therefore not returned. The HTTP-default run passed. This is environmental evidence, not a product fix.

## Remaining launch gates

- Docker and Docker Compose are absent in this environment. A clean Docker build/install has **not** been validated.
- These tests do not run application startup, provider login, live agent sessions or cross-device reconnection.
- A real desktop-to-phone recording remains pending; no storyboard is evidence of actual execution.
- Public promotional posts remain unsent pending explicit posting authorization and an appropriate account.

Use these precise limits when describing launch readiness. Do not label onboarding fully verified yet.
