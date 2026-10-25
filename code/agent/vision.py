"""Screenshots and PDFs -> Observations.

PDFs are read with `pdftotext` when installed; screenshots (and PDFs without a text
layer) go to Gemini vision once and the structured extraction is cached.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from . import llm
from .evidence import RESULT_STATE
from .model import RESOURCE_RE, classify_resource, keys_from_text

PROMPT = """This is an internal business artifact (an admin-console screenshot or a document) from the
online shop "Northstar Shop". List every data record it shows about a customer.

For each record return:
- reference: the record's identifier exactly as printed. Prefer a resource id such as account_N,
  search_N, support_N, analytics_N, export_N, data_flow_N, backup_N, audit_record_N,
  tombstone_N, legal_hold_N, marketing_N, object_N; otherwise the printed key (file name such as
  delivery_confirmation_19.pdf, statement number, user_N customer key, order ORD-..., ticket CS-...)
- other_references: any other identifiers printed for the same record (resource ids, file names,
  ticket ids, user_N)
- record_type: USER_PROFILE, SEARCH_INDEX_ENTRY, SUPPORT_MESSAGE, SUPPORT_ATTACHMENT,
  ANALYTICS_PROFILE, USER_EXPORT, DATA_FLOW, BACKUP_ARCHIVE, SECURITY_AUDIT_LOG,
  ACCOUNT_TOMBSTONE, LEGAL_HOLD_RECORD, MARKETING_PROFILE or OTHER
- state, judged only from what is visible:
  FOUND        the record exists (e.g. profile, index document, export file, stored object,
               statement, audit entry) and still shows the person's name/email/phone
  IDENTIFIABLE analytics/support/marketing record that still shows direct identifiers
  REDACTED     record exists but direct identifiers are removed/masked (only a stable key remains)
  ANONYMIZED   no link to a person remains
  ACTIVE       connector/sync enabled or running, or a backup archive that can still be restored
  DISABLED     connector disabled/stopped
  BLOCKED      restore blocked / not allowed
  NOT_FOUND    shown as deleted, missing or no results
- identifiers: every email, phone, user_N, person name or hash shown for that record
- observed_at: ISO-8601 time shown for that record if any, else ""
Return JSON: {"records": [ ... ]}"""

SCHEMA = {"type": "OBJECT", "properties": {"records": {"type": "ARRAY", "items": {
    "type": "OBJECT", "properties": {
        "reference": {"type": "STRING"}, "other_references": {"type": "ARRAY", "items": {"type": "STRING"}},
        "record_type": {"type": "STRING"},
        "state": {"type": "STRING"}, "identifiers": {"type": "ARRAY", "items": {"type": "STRING"}},
        "observed_at": {"type": "STRING"}},
    "required": ["reference", "record_type", "state", "identifiers"]}}}, "required": ["records"]}


def pdf_text(path: Path) -> str:
    if not shutil.which("pdftotext"):
        return ""
    return subprocess.run(["pdftotext", "-layout", str(path), "-"], capture_output=True, text=True).stdout


def alias_table(activity_rows) -> dict:
    """Data-catalog rows map printed aliases (file names, statement ids, user_N keys) to resources:
    'Data catalog linked export_1842 (NST-2026-0718-1842) to ...'"""
    out = {}
    for a in activity_rows:
        if a["event_type"] == "LINK_RESOURCE_IDENTITY":
            m = re.search(r"\b(\w+_\d+) \(([^)]+)\)", a["details"])
            if m:
                out[m.group(2).strip().lower()] = m.group(1)
                out[m.group(1).lower()] = m.group(1)
    return out


def resolve_reference(rec: dict, aliases: dict) -> str:
    refs = [rec.get("reference") or ""] + list(rec.get("other_references") or [])
    for ref in refs:                       # an explicit resource id wins
        ref = ref.strip()
        if RESOURCE_RE.fullmatch(ref):
            return ref
    for ref in refs:
        hit = aliases.get(ref.strip().lower())
        if hit:
            return hit
        for rid in RESOURCE_RE.findall(ref):
            return rid
    return ""


ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")
PII_STATE_TYPES = {"ANALYTICS_PROFILE", "SUPPORT_MESSAGE", "ACCOUNT_TOMBSTONE", "EMAIL_DELIVERY_LOG"}


def _extract(path: Path) -> list:
    records = []
    if llm.available():
        try:
            records = llm.generate_json(PROMPT, image_path=path, schema=SCHEMA).get("records", [])
        except Exception as exc:
            print(f"[vision] {path.name}: {exc}")
    if not records and path.suffix.lower() == ".pdf":
        records = _pdf_fallback(pdf_text(path))
    return records


def load_visual_evidence(store, activity_rows=(), verbose=False, workers=4):
    from concurrent.futures import ThreadPoolExecutor
    aliases = alias_table(activity_rows)
    files = [(fid, m, store.dataset / m["file_path"]) for fid, m in sorted(store.manifest.items())
             if (store.dataset / m["file_path"]).suffix.lower() in (".png", ".jpg", ".jpeg", ".pdf")]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        extracted = list(pool.map(lambda f: _extract(f[2]), files))
    for (fid, m, path), records in zip(files, extracted):
        by_rid = {}
        for r in records:
            rid = r.get("resource_id") or resolve_reference(r, aliases)
            if rid:
                by_rid.setdefault(rid, []).append(r)
        # a PDF that is itself an uploaded ticket document is a support *attachment*
        is_attachment = path.suffix.lower() == ".pdf" and "attachment" in pdf_text(path).lower()
        for rid, rows in by_rid.items():
            if is_attachment and rid.startswith("support_"):
                rows = [dict(r, record_type="SUPPORT_ATTACHMENT", state="FOUND") for r in rows]
            # an alias can be printed on several rows (e.g. a tombstone's linked records);
            # keep the rows whose type matches the resource itself
            native = classify_resource(rid)[1]
            same = [r for r in rows if (r.get("record_type") or "").upper() == native]
            for r in (same or rows[:1]):
                _add(store, fid, m, rid, r)
        if verbose:
            print(fid, path.name, len(records))


def _add(store, fid, m, rid, r):
    keys = set()
    for ident in r.get("identifiers") or []:
        keys |= keys_from_text(ident)
    rtype = (r.get("record_type") or "").upper()
    rtype = "" if rtype in ("", "OTHER") else rtype
    state = RESULT_STATE.get((r.get("state") or "FOUND").upper(), "PRESENT")
    direct = keys - {k for k in keys if k.startswith("user_")}
    if state == "PRESENT" and (rtype in PII_STATE_TYPES or classify_resource(rid)[1] in PII_STATE_TYPES):
        state = "IDENTIFIABLE" if direct else "REDACTED"
    when = r.get("observed_at") or ""
    when = when if ISO.match(when) and when <= m["captured_at"] else m["captured_at"]
    store.add(fid, m["captured_at"], when, rid, state, keys, "", rtype, "visual")


def _pdf_fallback(text: str):
    """Without an LLM: every resource id in the PDF is reported as FOUND with nearby identifiers."""
    out = []
    for rid in sorted(set(RESOURCE_RE.findall(text))):
        out.append({"resource_id": rid, "reference": rid, "record_type": "", "state": "FOUND",
                    "identifiers": sorted(keys_from_text(text))})
    return out
