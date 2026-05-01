# n8n Test Harness — Design

Behavioural test runner for `build_*.py` workflows. Goal: catch dedup bugs, OOM topology, recipient-list errors BEFORE pushing to thebonpet.app.n8n.cloud.

## Decisions (defaults — override and tell me)

| Decision | Default | Why |
|---|---|---|
| Runtime | Python (pytest) | Match existing `build_*.py` |
| Test format | YAML | Human-readable, no boilerplate |
| Local n8n | Docker via OrbStack | You already use OrbStack |
| WA/TG mocks | Flask servers on localhost | Smallest moving parts |
| Block prod imports | YES | `make test` must pass before any `python build_X.py` PUT to cloud |
| Test runtime per workflow | Target <30s | Use synthetic 5-customer fixtures, not real CSV exports |

## Architecture

```
fixture YAML  →  runner.py  →  POST webhook to local n8n (localhost:5678)
                                      │
                                      ▼
                              workflow runs
                                      │
                                      ▼
                              HTTP nodes call mocks/wa_mock.py (localhost:9999)
                                      │
                                      ▼
                              mock captures payloads to memory
                                      │
                                      ▼
                  runner.py queries mock /captured  →  asserts vs YAML expected
```

## Test spec format (see `specs/reorder-reminder.example.yml`)

Each spec declares:
- `workflow_id`: imported workflow on local n8n
- `fixtures`: rows to seed in the in-memory Customer Orders DB stub
- `trigger`: webhook URL + payload (or cron simulation)
- `expected.wa_messages`: array of `{phone, template, must_contain, must_not_contain}`
- `expected.cooldown_writes`: which sent_log rows should be added

Runner runs the workflow, queries `wa_mock`, asserts.

## What's built right now

- ✅ `mocks/wa_mock.py` — Flask receiver, captures POSTs to `/whatsapp/send`, exposes `/captured` and `/reset`
- ✅ `docker-compose.yml` — local n8n + sqlite, port 5678
- ✅ `Makefile` — `make up | down | test | reset-mocks | clean`
- ✅ `specs/reorder-reminder.example.yml` — sample spec showing format
- ✅ `runner.py` — core loop (load YAML → trigger → poll mock → assert). **n8n trigger HTTP call is stubbed** (TODO: webhook auth)
- ✅ `requirements.txt`

## What's NOT built (your call to confirm before I go further)

1. **`build_*.py --target local` flag** — existing scripts hardcode `thebonpet.app.n8n.cloud`. To test locally, scripts need a flag to PUT to `localhost:5678`. Smallest patch: env var `N8N_BASE_URL` defaults to cloud, override for tests. Want me to add to all `build_*.py`?
2. **Customer Orders DB stub** — currently real workflows read from Google Sheet `1GP0RBD...`. For tests, need an in-memory fixture loaded into a mock Sheets endpoint OR a flag to bypass and inject fixture rows directly. Latter is simpler — needs `_sheets_helpers.py` patch.
3. **Cron simulation** — workflows triggered by cron need a "fast-forward" mode. Likely solved by triggering the workflow's first node directly via webhook instead of cron.
4. **CI integration** — should `make test` run on every commit? Pre-push hook? Github Actions on `n8n-bonpet` repo (if you put it on GitHub)?

## Run order (once skeleton lands)

```bash
cd ~/n8n-bonpet/test-harness
pip install -r requirements.txt
make up                    # boots local n8n + WA mock
# manually import a workflow JSON into localhost:5678 admin UI for now
python runner.py specs/reorder-reminder.example.yml
make down
```

## Status: SKELETON — extend before relying on it
