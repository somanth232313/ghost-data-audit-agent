"""Deterministic parsers that turn every evidence format into Observations.

Supported: SQLite SQL snapshots (inventory, primary/replica, account and search
snapshots), JSON exports (service exports, snapshot bundles, record ledgers,
collections), plain-text handovers, and every log dialect in the dataset.
Images and PDFs are handled in vision.py and merged in by the loader.
"""
from __future__ import annotations

import csv
import json
import re
import sqlite3
from collections import defaultdict
from pathlib import Path

from .model import (
    ABSENT, ACTIVE, ANONYMIZED, BLOCKED, DISABLED, IDENTIFIABLE, PRESENT, REDACTED,
    RESOURCE_RE, Observation, classify_resource, keys_from_text,
)

RESULT_STATE = {
    "FOUND": PRESENT, "NOT_FOUND": ABSENT, "IDENTIFIABLE": IDENTIFIABLE,
    "ANONYMIZED": ANONYMIZED, "REDACTED": REDACTED, "ACTIVE": ACTIVE,
    "DISABLED": DISABLED, "BLOCKED": BLOCKED, "PRESENT": PRESENT, "ABSENT": ABSENT,
}

DIRECT_FIELDS = (
    "full_name", "name", "display_name", "email", "phone", "email_hash",
    "requester_name", "requester_email", "requester_phone", "owner_email",
    "owner_phone", "customer_email",
)


def truthy(v) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "y")


def _nonnull(v) -> bool:
    return v not in (None, "", "null", "NULL", "None", "[REDACTED]")


def record_state(category: str, rec: dict) -> str:
    """Derive the privacy state of a structured record from its fields."""
    if category == "DATA_FLOW":
        return ACTIVE if truthy(rec.get("enabled")) else DISABLED
    if category == "BACKUP_ARCHIVE":
        return ACTIVE if truthy(rec.get("restore_allowed")) else BLOCKED
    if category == "ANALYTICS_PROFILE" or category == "PRODUCT_TELEMETRY":
        ident = any(_nonnull(rec.get(f)) for f in ("email", "phone", "email_hash", "user_id", "customer_reference"))
        return IDENTIFIABLE if ident else ANONYMIZED
    if category in ("SUPPORT_MESSAGE", "ACCOUNT_TOMBSTONE", "EMAIL_DELIVERY_LOG"):
        ident = any(_nonnull(rec.get(f)) for f in DIRECT_FIELDS)
        text = rec.get("message_text") or rec.get("message_excerpt") or ""
        if keys_from_text(text) - {k for k in keys_from_text(text) if k.startswith("user_")}:
            ident = True
        return IDENTIFIABLE if ident else REDACTED
    return PRESENT


def _category_hint(collection: str, rec: dict, default: str) -> str:
    c = (collection or "").lower()
    if "attachment" in c or rec.get("storage_class") == "ticket_attachment" or (
        rec.get("file_name") and str(rec.get("resource_id", "")).startswith("support_")):
        return "SUPPORT_ATTACHMENT"
    return default


class EvidenceStore:
    def __init__(self, dataset: Path):
        self.dataset = dataset
        self.obs: list[Observation] = []
        self.coverage: dict[str, set] = defaultdict(set)   # file_id -> systems covered
        self.links: list[tuple[str, str, str, str]] = []   # (file_id, captured_at, key, resource) explicit links
        self.manifest = {r["file_id"]: r for r in csv.DictReader(open(dataset / "evidence_files.csv"))}

    # ------------------------------------------------------------------ helpers
    def add(self, fid, cap, when, rid, state, keys=(), system="", category="", source="", note=""):
        sys_, cat = classify_resource(rid)
        self.obs.append(Observation(
            file_id=fid, captured_at=cap, observed_at=when or cap, resource_id=rid,
            system_id=system or sys_, category=category or cat, state=state,
            keys=set(keys), source=source, note=note))
        self.coverage[fid].add(system or sys_)

    def add_record(self, fid, cap, when, rec: dict, collection="", system="", source=""):
        rid = rec.get("resource_id")
        if not rid:
            return
        _, cat = classify_resource(rid)
        cat = _category_hint(collection, rec, cat)
        state = record_state(cat, rec)
        self.add(fid, cap, when, rid, state, keys_from_text(rec), system, cat, source)

    def add_lookup(self, fid, cap, rec: dict, source=""):
        """record_type/result/subject_reference style rows."""
        rid = rec.get("resource_id")
        if not rid:
            return
        state = RESULT_STATE.get(str(rec.get("result", "")).upper(), PRESENT)
        subj = rec.get("subject_reference") or rec.get("lookup_value")
        self.add(fid, cap, rec.get("recorded_at") or cap, rid, state, keys_from_text(subj),
                 rec.get("system", ""), rec.get("record_type", ""), source)

    # ------------------------------------------------------------------ loading
    def load_all(self):
        for fid, m in sorted(self.manifest.items()):
            path = self.dataset / m["file_path"]
            cap = m["captured_at"]
            ext = path.suffix.lower()
            try:
                if ext == ".sql":
                    self.load_sql(fid, cap, path)
                elif ext == ".json":
                    self.load_json(fid, cap, path)
                elif ext == ".log":
                    self.load_log(fid, cap, path)
                elif ext == ".txt":
                    self.load_text(fid, cap, path)
            except Exception as exc:  # keep going; a broken file must not kill the run
                print(f"[warn] failed to parse {path.name}: {exc}")
        return self

    # SQL -------------------------------------------------------------------------
    def load_sql(self, fid, cap, path: Path):
        con = sqlite3.connect(":memory:")
        con.executescript(path.read_text())
        tables = [r[0] for r in con.execute("select name from sqlite_master where type='table'")]
        for t in tables:
            cols = [c[1] for c in con.execute(f'pragma table_info("{t}")')]
            rows = [dict(zip(cols, r)) for r in con.execute(f'select * from "{t}"')]
            if t == "snapshot_scope":
                for r in rows:
                    self.coverage[fid].add(r["origin_system"])
                continue
            source = t
            if "record_json" in cols:                       # generic primary / replica tables
                for r in rows:
                    rec = json.loads(r["record_json"])
                    self.coverage[fid].add(r["origin_system"])
                    self.add_record(fid, cap, r.get("updated_at"), rec, r.get("collection_name", ""),
                                    r["origin_system"], source)
                continue
            # typed tables
            for r in rows:
                rec = dict(r)
                if "user_id" in rec and "customer_reference" not in rec:
                    rec["customer_reference"] = rec.pop("user_id")
                when = rec.get("updated_at") or rec.get("indexed_at")
                self.add_record(fid, cap, when, rec, t, "", source)
                if rec.get("source_resource_id"):           # search doc points to its source account
                    self.links.append((fid, cap, rec["source_resource_id"], rec["resource_id"]))
            # mark coverage from table name even if empty
            sys_guess = {
                "customer_profiles": "account_service", "search": "search_service",
                "analytics": "analytics_platform", "support": "support_platform",
                "export": "export_service", "connector": "integration_service",
                "backup": "backup_service", "security": "security_service",
            }
            for k, s in sys_guess.items():
                if k in t:
                    self.coverage[fid].add(s)

    # JSON ------------------------------------------------------------------------
    def load_json(self, fid, cap, path: Path):
        d = json.loads(path.read_text())
        if "service_exports" in d:
            for system, groups in d["service_exports"].items():
                self.coverage[fid].add(system)
                for coll, items in groups.items():
                    for it in items:
                        self._service_export_item(fid, cap, system, coll, it)
        if "snapshots" in d:
            for s in d["snapshots"]:
                when = s.get("captured_at") or cap
                store = s.get("data_store", "")
                if s.get("collection") == "operational_record_history":
                    for r in s["records"]:
                        self.add_lookup(fid, cap, r, store)
                    continue
                self.coverage[fid].add(store)
                for r in s["records"]:
                    self.add_record(fid, cap, when, r, s.get("collection", ""), store, store)
        elif "records" in d:
            for r in d["records"]:
                if "record_type" in r or "result" in r:
                    self.add_lookup(fid, cap, r, d.get("origin", ""))
                else:
                    self.add_record(fid, cap, d.get("captured_at") or cap, r,
                                    d.get("collection", ""), "", d.get("source", ""))

    def _service_export_item(self, fid, cap, system, coll, it):
        when = it.get("observed_at") or cap
        rid = it.get("resource_id")
        if coll == "profile_lookups":
            prof = it.get("profile")
            if it.get("status_code") == 404 or not prof:
                self.add(fid, cap, when, rid, ABSENT, (), system, "", "service_export")
            else:
                self.add(fid, cap, when, rid, PRESENT, keys_from_text(prof), system, "", "service_export")
            return
        if coll == "index_queries":
            docs = (it.get("hits") or {}).get("documents") or []
            qkeys = keys_from_text(it.get("query"))
            if not docs:
                if rid:
                    self.add(fid, cap, when, rid, ABSENT, qkeys, system, "", "service_export")
                return
            for doc in docs:
                self.add(fid, cap, when, doc.get("resource_id") or rid, PRESENT,
                         keys_from_text(doc) | qkeys, system, "", "service_export")
            return
        rec = dict(it)
        self.add_record(fid, cap, when, rec, coll, system, "service_export")

    # Text handover -----------------------------------------------------------------
    def load_text(self, fid, cap, path: Path):
        for line in path.read_text().splitlines():
            parts = [p.strip() for p in line.lstrip("- ").split("|")]
            if len(parts) >= 6 and RESOURCE_RE.fullmatch(parts[2]):
                when, system, rid, rtype, result, subj = parts[:6]
                state = RESULT_STATE.get(result.upper(), PRESENT)
                self.add(fid, cap, when, rid, state, keys_from_text(subj), system, rtype, "handover")

    # Logs ----------------------------------------------------------------------------
    KV = re.compile(r'(\w+)=("([^"]*)"|[^\s|]+)')
    TRACE = re.compile(r"\b(tr-[\w-]+|rb-[\w-]+)\b")
    TS = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)")
    LINK_FIELDS = ("lookup_key", "match_key", "subject_key", "scan_key", "owner_key", "subject_ref",
                   "correlation_key", "matched_value", "email", "customer_email", "customer_reference",
                   "requester_email", "requester_name", "requester_phone", "owner_email",
                   "subject_reference", "phone", "email_hash")

    def parse_log_line(self, line: str):
        line = line.replace('\\"', '"')
        m = self.TS.match(line)
        if not m:
            return None
        kv = {}
        for g in self.KV.finditer(line):
            kv[g.group(1)] = g.group(3) if g.group(3) is not None else g.group(2)
        event = kv.get("event")
        if not event:
            em = re.search(r"\] ([a-z_]+):", line) or re.search(r"\| ([a-z_]+) \|", line)
            event = em.group(1) if em else ""
        tm = self.TRACE.search(line)
        svc = kv.get("service") or kv.get("function") or kv.get("logger")
        if not svc:
            sm = re.search(r"\[(\w+_\w+)\]", line) or re.search(r"\| (\w+_(?:service|platform)) \|", line) \
                or re.search(r" (\w+_(?:service|platform|replica)) tr-", line)
            svc = sm.group(1) if sm else ""
        return {"ts": m.group(1), "event": event, "trace": tm.group(1) if tm else None, "svc": svc, "kv": kv}

    def load_log(self, fid, cap, path: Path):
        recs = [r for r in (self.parse_log_line(l) for l in path.read_text().splitlines()) if r]
        trace_keys: dict[str, set] = defaultdict(set)
        for r in recs:
            if r["trace"]:
                for f in self.LINK_FIELDS:
                    if f in r["kv"]:
                        trace_keys[r["trace"]] |= keys_from_text(r["kv"][f])
                        if RESOURCE_RE.fullmatch(r["kv"][f] or ""):
                            trace_keys[r["trace"]].add(r["kv"][f])
        for r in recs:
            kv, ev = r["kv"], r["event"]
            rid = kv.get("resource_id")
            if not rid or not RESOURCE_RE.fullmatch(rid):
                continue
            keys = set()
            for f in self.LINK_FIELDS:
                if f in kv:
                    keys |= keys_from_text(kv[f])
            if r["trace"]:
                keys |= {k for k in trace_keys[r["trace"]] if k != rid}
            state = self._log_state(ev, kv, rid)
            if state is None:
                continue
            _, cat = classify_resource(rid)
            if kv.get("record_type"):
                cat = kv["record_type"]
            elif ev.startswith("attachment") or (kv.get("file") and rid.startswith("support_")):
                cat = "SUPPORT_ATTACHMENT"
            self.add(fid, cap, r["ts"], rid, state, keys, kv.get("system", ""), cat, "log:" + ev)

    def _log_state(self, ev, kv, rid):
        if ev == "record_lookup":
            return RESULT_STATE.get(kv.get("result", "").upper(), PRESENT)
        if ev in ("profile_lookup_completed", "contact_query_completed", "contact_lookup_completed",
                  "audit_record_read_completed"):
            if kv.get("rows") == "0" or kv.get("http_status") == "404":
                return ABSENT
            if rid.startswith("account_") and any(_nonnull(kv.get(f)) for f in ("email", "phone")) is False \
                    and "email" in kv and "customer_reference" in kv:
                return PRESENT
            return PRESENT
        if ev == "index_query_completed":
            return PRESENT if kv.get("result") == "hit" else ABSENT
        if ev in ("message_pii_scan", "analytics_identity_scan"):
            return IDENTIFIABLE
        if ev == "attachment_pii_scan":
            return PRESENT
        if ev == "analytics_profile_read":
            return IDENTIFIABLE if kv.get("direct_identifiers", "0") != "0" else ANONYMIZED
        if ev == "analytics_profile_read_completed":
            return record_state("ANALYTICS_PROFILE", kv)
        if ev == "support_ticket_read_completed":
            return record_state("SUPPORT_MESSAGE", kv)
        if ev == "message_read":
            return record_state("SUPPORT_MESSAGE", {"message_text": kv.get("message_text")})
        if ev in ("connector_config_read_completed", "connector_heartbeat", "connector_config_loaded"):
            return ACTIVE if truthy(kv.get("enabled")) else DISABLED
        if ev == "restore_policy_read_completed":
            return ACTIVE if truthy(kv.get("restore_allowed")) else BLOCKED
        if ev == "restore_candidate_evaluated":
            return ACTIVE if truthy(kv.get("eligible")) and not truthy(kv.get("deletion_block")) else BLOCKED
        if ev == "restore_authorization_completed":
            return BLOCKED if kv.get("decision", "").lower() == "deny" else ACTIVE
        if ev in ("export_metadata_read_completed", "export_download_check_completed"):
            return PRESENT if truthy(kv.get("available")) else ABSENT
        if ev == "deletion_marker_check":
            return PRESENT if not truthy(kv.get("marker_found")) else ABSENT
        return None
