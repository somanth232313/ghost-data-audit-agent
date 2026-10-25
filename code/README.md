# Ghost Data Still Exists — Deletion Audit Agent

An evidence-driven agent that decides, for each deletion request, whether a person's data is
really gone (`COMPLETE`), still present (`INCOMPLETE`), or out of scope (`NOT_APPLICABLE`), and
emits the ordered findings → remediation → verification plan required by the challenge.

## Quick start

```bash
# Python 3.10+; no third-party packages required (stdlib only). Optional: poppler's `pdftotext`.
cp .env.example .env          # then put your key in .env  (never commit it)
python3 code/main.py          # -> output.csv at the repository root (518 rows)

# deterministic mode, no API key needed (rule-based intent + pdftotext; screenshots skipped)
python3 code/main.py --no-llm

# evaluate against the 32 labelled samples
python3 code/main.py --input dataset/sample_deletion_requests.csv --output sample_pred.csv
python3 code/evaluation/main.py --pred sample_pred.csv --gold dataset/sample_output.csv -v

# team-authored synthetic cases
python3 code/evaluation/custom_cases.py
```

`.env` keys: `GEMINI_API_KEY` (required for LLM features), `GEMINI_MODEL` (default
`gemini-3.5-flash`), optional `GEMINI_PRICE_IN_PER_M` / `GEMINI_PRICE_OUT_PER_M` for cost estimates.
LLM responses are cached in `code/.cache/llm/`, so re-runs are free and reproducible.

## Architecture

```
dataset/ ──► evidence.py  ── SQL / JSON / logs / text ──┐
             vision.py    ── PNG / PDF (Gemini vision) ─┤──► Observation(resource, system, category,
                                                         │     state, identifiers, file_id, times)
identity.py ── current + historical identifiers ────────┤
intent.py   ── does the request authorise deletion? ────┤   (Gemini, batched; rule fallback)
engine.py   ── link → reconstruct state @audit_at → apply deletion_rules.csv → order steps
main.py     ── output.csv + evaluation/usage_report.md
```

**Design principle:** the LLM is only used where language or pixels must be understood
(multilingual intent, screenshots). Everything that must be exact — ids, timestamps, policy,
ordering — is deterministic code, so the output is reproducible and auditable.

### 1. Evidence normalisation (`agent/evidence.py`, `agent/vision.py`)
Every format becomes the same `Observation`:
- **SQL snapshots** (inventory, primary / read / reporting / regional / failover replicas, account and
  search snapshots) are executed in in-memory SQLite. Each table is kept as a separate *copy* so
  replication drift is visible. `snapshot_scope` and table presence record which systems a capture covers.
- **JSON** service exports, snapshot bundles, record ledgers, collections.
- **Logs** in five dialects (key=value, syslog, bracketed traces, pipe-delimited, cloud JSON-ish). Lines are
  grouped by `trace_id` so a `lookup_key=<email>` line links to the `resource_id` returned later in the trace.
- **Text handovers** (`| system | resource | TYPE | RESULT | subject`).
- **Screenshots / PDFs**: Gemini returns structured records; printed aliases (file names,
  statement ids, `user_N` keys) are mapped to resource ids through data-catalog
  `LINK_RESOURCE_IDENTITY` activity rows. PDFs also have a `pdftotext` fallback.

State is derived from content, never from `reported_result`: e.g. a support ticket with requester
email → `IDENTIFIABLE`; analytics row without direct identifiers → `ANONYMIZED`; `enabled=1` → `ACTIVE`.

### 2. Identity resolution (`agent/identity.py`)
`user_identities.csv` + `user_audit_log.csv` give every email (case-insensitive), phone (digits only),
email hash and `user_id` the person ever had. A request email may be current or historical.

### 3. Linking resources to the subject (`agent/engine.py`)
1. **Identifier match** — any observation carrying one of the subject's identifiers (incl. trace-linked keys).
2. **Explicit links** — search document `source_resource_id`, data-catalog rows.
3. **Deletion-worker batch** — deleted / anonymised records carry no identifiers. They are tied to a
   case through the worker activity on the case's `reported_at` day, after removing resources keyed to
   anyone, cases that already own a keyed resource of that category, and matching the account
   deletion result to the reported status (SUCCESS↔COMPLETE, FAILED↔NOT_STARTED, PARTIAL↔PARTIALLY_COMPLETE).
4. Resources processed for the same person's *other* request are excluded.

### 4. State at `audit_at`
Only captures with `captured_at ≤ audit_at` (and facts observed before it) are used. Within the latest
capture the latest fact per copy is taken and the **worst copy wins** (a stale replica still exposes data).
If an account has no direct observation, the latest capture covering `account_service` that does not
contain it proves absence.

### 5. Policy and plan
`deletion_rules.csv` is loaded at runtime. Each (category, state) maps to either a violation
(finding + remediation + post-check) or a compliant check:

| Category | Violation → fix | Compliant check |
|---|---|---|
| profile, search, export, attachment, marketing, cache | REMAINING → DELETE | VERIFY_ABSENT |
| support message, email log, tombstone | EXPOSED_PII → REDACT | VERIFY_REDACTED |
| analytics, telemetry | EXPOSED_PII → ANONYMIZE | VERIFY_ANONYMIZED |
| data flow | ACTIVE_DATA_FLOW → DISABLE | VERIFY_DISABLED |
| backup archive (retain) | RESTORE_RISK → BLOCK | VERIFY_BLOCKED |
| security audit, legal hold (retain) | — | VERIFY_PRESENT |

`INCOMPLETE` plans verify every remediated item, the primary account and the retained records, with
empty `evidence_id`. `COMPLETE` plans list only evidence-backed checks. Steps are sorted by phase,
then the operation order of the problem statement, then `system_id`, `resource_id`, so data flows are
disabled and restores blocked before records are changed.

## Files

| Path | Purpose |
|---|---|
| `code/main.py` | entry point, writes `output.csv` and `evaluation/usage_report.md` |
| `code/agent/*.py` | evidence parsers, vision, identity, intent, engine, Gemini client |
| `code/evaluation/main.py` | scorer: status, step P/R/F1, field and sequence accuracy |
| `code/evaluation/custom_cases.py` | 5 synthetic team-authored cases |
| `code/evaluation/evaluation_report.md` | results, failure analysis, improvements |
| `code/evaluation/usage_report.md` | tokens and cost of the final run |

Prompts live in `agent/intent.py` (`PROMPT`) and `agent/vision.py` (`PROMPT`).
