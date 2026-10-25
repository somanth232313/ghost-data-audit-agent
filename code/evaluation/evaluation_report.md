# Evaluation Report — Ghost Data Still Exists

All numbers are produced by `code/evaluation/main.py` against `dataset/sample_output.csv`
(the 32 labelled public cases) and by `code/evaluation/custom_cases.py` (team-authored cases).

```bash
python3 code/main.py --input dataset/sample_deletion_requests.csv --output sample_pred.csv
python3 code/evaluation/main.py --pred sample_pred.csv -v
python3 code/evaluation/custom_cases.py
```

## Metric definitions

| Metric | Definition |
|---|---|
| Case status accuracy | `deletion_status` exact match |
| Step precision / recall / F1 | steps matched as a multiset on (phase, system_id, resource_id, operation, state, evidence_id) |
| Field accuracy | position-aligned per-field agreement (step *i* of prediction vs step *i* of gold) |
| Sequence accuracy | the whole `steps_json` array is identical, in order, with correct `step_no`, and status matches |

## Results on the 32 labelled samples (final system)

| Metric | Full system (Gemini + deterministic engine) | Deterministic only (`--no-llm`) |
|---|---:|---:|
| **Case status accuracy** | **100.0 %** (32/32) | 93.8 % |
| Step precision | **99.5 %** | 98.9 % |
| Step recall | **95.3 %** | 90.7 % |
| **Step F1** | **0.974** | 0.946 |
| **Sequence accuracy** | **75.0 %** (24/32) | 71.9 % |

Field-level accuracy (full system, position-aligned):

| phase | system_id | resource_id | operation | state | evidence_id |
|---:|---:|---:|---:|---:|---:|
| 95.9 % | 91.7 % | 91.2 % | 92.2 % | 92.2 % | 95.9 % |

Recall by phase: **FINDING 41/41 (100 %)**, **REMEDIATION 41/41 (100 %)**, VERIFICATION 102/111 (91.9 %).
Status confusion: COMPLETE→COMPLETE 10, INCOMPLETE→INCOMPLETE 22, no errors. Every recorded
violation is found and remediated in the correct order; all remaining misses are verification checks.

### Full dataset run (518 requests, no labels)
`INCOMPLETE` 338, `COMPLETE` 170, `NOT_APPLICABLE` 10. An automatic contract check (exact header,
518 rows, natural `case_id` order, consecutive `step_no`, `[]` for NOT_APPLICABLE, evidence only on
findings for INCOMPLETE, evidence on every COMPLETE check) reports **0 violations**. The Gemini intent
classifier and the rule fallback agree on all 518 requests.

## Team-authored evaluation cases (`custom_cases.py`) — 5/5 pass

Each case builds its own miniature dataset and runs the real CLI end to end.

| # | Case | What it tests | Expected |
|---|---|---|---|
| C1 | Historical email in search index | identity resolution through `user_audit_log.csv`; account linked via worker activity only | INCOMPLETE: REMAINING search → DELETE → verify account + search absent |
| C2 | Leftover profile only in a capture taken **after** `audit_at` | time cutoff — later evidence must not change the answer | COMPLETE with `[]` |
| C3 | Pending profile + enabled connector + restorable backup + retained audit record + look-alike decoy (`tomas.lindqvist@…`) | ordering (DISABLE → BLOCK → DELETE), retention (VERIFY_PRESENT), decoy rejection | INCOMPLETE, 10 ordered steps |
| C4 | Primary table clean, failover replica still holds the profile | replication drift: worst copy wins | INCOMPLETE: REMAINING account |
| C5 | "Download my information… keep the account" | non-deletion intent | NOT_APPLICABLE `[]` |

## Failures found and improvements made

Iterations on the samples (each row is a measured run):

| Version | Change | Status acc. | Step F1 | Seq. acc. |
|---|---|---:|---:|---:|
| v1 | Parsers for all SQL/JSON/log/text formats, identifier linking, rule engine, ordering | 84.4 % | 0.850 | 28.1 % |
| v2 | Rule intent was fooled by "apply the **correct** treatment" / "remaining **copy of** my profile" → tightened negation patterns. INCOMPLETE plans now always verify the primary account even with no direct observation | 93.8 % | 0.926 | 59.4 % |
| v3 | Deleted/anonymised records carry no identifiers → **deletion-worker batch linking** on the `reported_at` day with elimination and *result↔reported_status* matching (raised account attribution from 14/29 to 22/29 in a back-test); excluded resources processed for the same person's other request; retained audit records count as future checks | 93.8 % | 0.946 | 71.9 % |
| v4 | `pdftotext` fallback for PDFs (support attachment `support_001` found) | 96.9 % | 0.960 | — |
| v5 | Gemini vision for screenshots with **alias resolution** via data-catalog rows (`delivery_confirmation_19.pdf → object_19`, `NST-2026-0718-1842 → export_1842`, `user_17 → tombstone_17`); Gemini multilingual intent; PDF "attachment" documents → SUPPORT_ATTACHMENT (delete, not redact); evidence predating a request that falls inside the person's **earlier** request window is attributed to that request | **100 %** | **0.974** | **75.0 %** |

Other failures found and fixed along the way:
- **LLM over-strictness.** The first intent prompt labelled "please *verify* my account was deleted" as
  non-deletion (4 sample cases). The prompt now states that verification/confirmation of an earlier
  erasure is a deletion request; disagreements with the rule classifier are printed every run.
- **Model availability / quota.** `gemini-2.5-flash` is retired for new keys and the free tier allows
  20 requests per model per day. The client now falls back through a model chain on daily-quota errors,
  checks the cache of every model first, and batches 75 unique request texts per call (5 intent calls for 518 requests).
- **Vision row explosion.** A tombstone console prints the alias `user_17` on every linked-record row;
  only rows whose type matches the resource are kept, and a tombstone showing only the stable key is `REDACTED`.
- **Replica handling.** Each SQL table is treated as a separate copy and the worst state wins, so a stale
  failover row is reported even when the primary is clean (sample case_05, custom C4).

## Known limitations (remaining 9 missed verification steps)

Verified with `grep` over `dataset/evidence` and `system_activity.csv`:
- 3 gold steps reference resources that appear **nowhere** in participant data
  (`account_004`, `audit_record_6645`, `audit_record_5569`) — not recoverable from evidence.
- 1 (`audit_record_4902`) appears only in a log captured after `audit_at`, with no subject identifier.
- 4 need attribution of identifier-free records (`analytics_901`, `analytics_1895`, `analytics_4566`,
  `account_410`) when several cases share one deletion-worker batch with the same reported status; ties are
  broken deterministically by (reported_at, case number), which is right in most but not all batches.
- 1 account (`account_008`) whose worker result (SUCCESS) disagrees with the reported status
  (PARTIALLY_COMPLETE), so the status-matching rule rejects it.
