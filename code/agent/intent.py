"""Does the request authorize deletion?  LLM (batched, multilingual) + rule fallback.

The deterministic rules always run; the LLM decides when available, and any
disagreement is printed so it can be reviewed in the evaluation report.
"""
from __future__ import annotations

import re

from . import llm

BATCH = 75

PROMPT = """You review privacy requests sent to an online shop. For each request decide whether the
person is AUTHORIZING DELETION / ERASURE / CLOSURE of their account or personal data
(fully or partially: e.g. delete the account, remove leftover copies, delete an export file,
remove a search result, close the account, disable integrations as part of closing it).

Requests that ask you to CHECK, VERIFY, AUDIT or CONFIRM that an earlier deletion/closure really
happened ("verify my account was deleted", "confirm nothing identifiable remains", "the privacy page
says my request failed, check whether it was removed") ARE deletion requests: the person asked for
erasure and wants it completed/verified.

It is NOT a deletion request when the person asks for access, a copy/export/download of data,
a correction or update of details (name, phone, email), a marketing opt-out / unsubscribe,
invoice copies, or explicitly wants the account to stay active / records kept.
Requests may be in any language.

Return a JSON array with one object per request, in the same order:
{{"id": "<id>", "deletion": true|false, "reason": "<= 12 words"}}

Requests:
{items}
"""

SCHEMA = {
    "type": "ARRAY",
    "items": {"type": "OBJECT", "properties": {
        "id": {"type": "STRING"}, "deletion": {"type": "BOOLEAN"}, "reason": {"type": "STRING"}},
        "required": ["id", "deletion"]},
}

DELETE_WORDS = [
    r"delet", r"eras", r"\bremove", r"close (my|this|the) account", r"closed my account",
    r"suppression", r"supprim", r"eliminaci", r"elimin", r"exclus", r"lösch", r"verwijder",
    r"削除", r"삭제", r"cancella", r"borra", r"usuń", r"stäng", r"rimuov", r"disconnect",
]
NON_DELETE = [
    r"without (removing|deleting)", r"keep the account", r"leave (the|my) (account|profile)",
    r"continue using", r"download my", r"copy of (last|my (invoices|data|records))", r"correct (it|my|the)\b(?! treatment)",
    r"misspelled", r"change the email", r"replace it",
    r"unsubscribe", r"opt[- ]out", r"stop (sending|marketing)", r"export my data", r"update my",
    r"keep (it|the account) active", r"as it is", r"don't delete", r"do not delete", r"not delete",
    r"not asking for deletion", r"do not close", r"marketing lists? only", r"not a request to erase",
    r"information request", r"what personal information you hold", r"promotional emails",
    r"resetting the login", r"don't (delete|change)",
]


def rule_intent(text: str) -> bool:
    t = text.lower()
    if any(re.search(p, t) for p in NON_DELETE):
        return False
    return any(re.search(p, t) for p in DELETE_WORDS)


def classify_requests(requests: list[dict], use_llm: bool = True) -> dict[str, bool]:
    rules = {r["case_id"]: rule_intent(r["request_text"]) for r in requests}
    if not (use_llm and llm.available()):
        return rules
    # identical texts are classified once (the dataset reuses many templates)
    uniq = sorted({r["request_text"] for r in requests})
    verdict: dict[str, bool] = {}
    for i in range(0, len(uniq), BATCH):
        chunk = uniq[i:i + BATCH]
        items = "\n".join(f'- id: "t{j}" text: {json_str(t)}' for j, t in enumerate(chunk))
        try:
            out = llm.generate_json(PROMPT.format(items=items), schema=SCHEMA)
            by_id = {o["id"]: bool(o["deletion"]) for o in out}
            for j, t in enumerate(chunk):
                verdict[t] = by_id.get(f"t{j}", rule_intent(t))
        except Exception as exc:   # network / quota problems -> deterministic fallback
            print(f"[intent] LLM batch failed ({exc}); using rules for {len(chunk)} texts")
            for t in chunk:
                verdict[t] = rule_intent(t)
    result = {r["case_id"]: verdict[r["request_text"]] for r in requests}
    diff = [c for c in result if result[c] != rules[c]]
    if diff:
        print(f"[intent] LLM and rules disagree on {len(diff)} cases: {diff[:10]}")
    return result


def json_str(s: str) -> str:
    import json
    return json.dumps(s, ensure_ascii=False)
