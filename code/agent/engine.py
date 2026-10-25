"""Deletion-audit decision engine.

For one request:  resolve identity -> discover linked resources -> reconstruct each
resource's state at audit_at -> apply deletion_rules.csv -> emit ordered steps.
"""
from __future__ import annotations

import csv
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from .evidence import EvidenceStore
from .identity import IdentityIndex, Subject
from .model import (
    ABSENT, ACTIVE, ANONYMIZED, BLOCKED, DISABLED, IDENTIFIABLE, PRESENT, REDACTED,
    Observation, classify_resource,
)

# Required treatment per data category (mirrors dataset/deletion_rules.csv).
DEFAULT_RULES = {
    "USER_PROFILE": "DELETE", "SECONDARY_IDENTIFIER": "DELETE", "SEARCH_INDEX_ENTRY": "DELETE",
    "CACHE_ENTRY": "DELETE", "SUPPORT_MESSAGE": "REDACT", "SUPPORT_ATTACHMENT": "DELETE",
    "ANALYTICS_PROFILE": "ANONYMIZE", "PRODUCT_TELEMETRY": "ANONYMIZE", "USER_EXPORT": "DELETE",
    "MARKETING_PROFILE": "DELETE", "EMAIL_DELIVERY_LOG": "REDACT", "SECURITY_AUDIT_LOG": "RETAIN",
    "LEGAL_HOLD_RECORD": "RETAIN", "BACKUP_ARCHIVE": "RETAIN", "ACCOUNT_TOMBSTONE": "REDACT",
    "DATA_FLOW": "DISABLE",
}

OP_ORDER = [
    "REPORT_REMAINING_RECORD", "REPORT_EXPOSED_PII", "REPORT_ACTIVE_DATA_FLOW", "REPORT_RESTORE_RISK",
    "DISABLE_DATA_FLOW", "BLOCK_RESTORE", "DELETE_RECORD", "REDACT_RECORD", "ANONYMIZE_RECORD",
    "VERIFY_RECORD_ABSENT", "VERIFY_RECORD_PRESENT", "VERIFY_RECORD_REDACTED",
    "VERIFY_RECORD_ANONYMIZED", "VERIFY_DATA_FLOW_DISABLED", "VERIFY_RESTORE_BLOCKED",
]
PHASE = {op: ("FINDING" if op.startswith("REPORT") else "VERIFICATION" if op.startswith("VERIFY")
              else "REMEDIATION") for op in OP_ORDER}
OP_STATE = {
    "REPORT_REMAINING_RECORD": "PRESENT", "REPORT_EXPOSED_PII": "PRESENT",
    "REPORT_ACTIVE_DATA_FLOW": "ACTIVE", "REPORT_RESTORE_RISK": "ACTIVE",
    "DISABLE_DATA_FLOW": "DISABLED", "BLOCK_RESTORE": "BLOCKED", "DELETE_RECORD": "ABSENT",
    "REDACT_RECORD": "REDACTED", "ANONYMIZE_RECORD": "ANONYMIZED", "VERIFY_RECORD_ABSENT": "ABSENT",
    "VERIFY_RECORD_PRESENT": "PRESENT", "VERIFY_RECORD_REDACTED": "REDACTED",
    "VERIFY_RECORD_ANONYMIZED": "ANONYMIZED", "VERIFY_DATA_FLOW_DISABLED": "DISABLED",
    "VERIFY_RESTORE_BLOCKED": "BLOCKED",
}
STATUS_RESULT = {"SUCCESS": "COMPLETE", "FAILED": "NOT_STARTED", "PARTIAL": "PARTIALLY_COMPLETE"}

# Worst-first ranking used when one capture holds several copies (primary vs replica).
SEVERITY = {IDENTIFIABLE: 5, ACTIVE: 5, PRESENT: 4, REDACTED: 2, ANONYMIZED: 2,
            DISABLED: 1, BLOCKED: 1, ABSENT: 0}


def ts(s: str) -> datetime:
    return datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")


def load_rules(dataset: Path) -> dict:
    rules = dict(DEFAULT_RULES)
    for r in csv.DictReader(open(dataset / "deletion_rules.csv")):
        rules[r["data_category"]] = r["required_action"]
    return rules


class Auditor:
    def __init__(self, dataset: Path, store: EvidenceStore, ids: IdentityIndex, all_requests: list[dict]):
        self.dataset, self.store, self.ids = dataset, store, ids
        self.rules = load_rules(dataset)
        self.activity = list(csv.DictReader(open(dataset / "system_activity.csv")))
        self.obs_by_res: dict[str, list[Observation]] = defaultdict(list)
        for o in store.obs:
            self.obs_by_res[o.resource_id].append(o)
        self._build_ownership()
        self._build_activity_links(all_requests)

    # ------------------------------------------------------------ ownership
    def _build_ownership(self):
        """resource -> users linked by any identifier in any observation (evidence <= audit
        filtering happens per case; ownership itself is time-invariant)."""
        self.owner_obs: dict[str, dict[str, str]] = defaultdict(dict)   # res -> user -> earliest capture
        for o in self.store.obs:
            for u in self.ids.owners(o.keys):
                prev = self.owner_obs[o.resource_id].get(u)
                if prev is None or o.captured_at < prev:
                    self.owner_obs[o.resource_id][u] = o.captured_at
        # explicit links: search doc -> source account, data catalog activity rows
        for fid, cap, src, dst in self.store.links:
            for u, c in list(self.owner_obs.get(dst, {}).items()):
                self.owner_obs[src].setdefault(u, c)
        for a in self.activity:
            if a["event_type"] == "LINK_RESOURCE_IDENTITY":
                for u in self.ids.owners(set(_keys(a["details"]))):
                    self.owner_obs[a["resource_id"]].setdefault(u, a["event_time"])

    def _build_activity_links(self, requests: list[dict]):
        """Resources with no identifier left (deleted / anonymised) are tied to a case through
        the deletion-worker batch that ran on the case's reported_at day.

        Within a day: resources already keyed to someone are removed, cases that already own a
        keyed resource of that category are removed, and the account deletion result must agree
        with the workflow's reported status (SUCCESS<->COMPLETE, FAILED<->NOT_STARTED,
        PARTIAL<->PARTIALLY_COMPLETE). Remaining ties are broken by (reported_at, case number)."""
        self.activity_owner: dict[str, set] = {}
        self.worker_days: dict[str, set] = defaultdict(set)   # resource -> days touched by the worker
        by_day = defaultdict(list)
        for a in self.activity:
            if a["source_system"] == "deletion_worker":
                by_day[a["event_time"][:10]].append(a)
                self.worker_days[a["resource_id"]].add(a["event_time"][:10])
        cases_by_day = defaultdict(list)
        self.user_report_days: dict[str, set] = defaultdict(set)
        self.requests_by_user: dict[str, list] = defaultdict(list)
        for r in requests:
            subj = self.ids.resolve(r["email"])
            if subj:
                cases_by_day[r["reported_at"][:10]].append((r, subj))
                self.user_report_days[subj.user_id].add(r["reported_at"][:10])
                self.requests_by_user[subj.user_id].append(r)
        for day, cases in cases_by_day.items():
            acts = sorted(by_day.get(day, []), key=lambda a: a["event_time"])
            order = sorted(cases, key=lambda x: (x[0]["reported_at"], _num(x[0]["case_id"])))
            pool = defaultdict(list)
            for a in acts:
                rid = a["resource_id"]
                if self.owner_obs.get(rid) or any(rid == p[0] for p in pool[classify_resource(rid)[1]]):
                    continue
                pool[classify_resource(rid)[1]].append((rid, a["reported_result"]))
            act_time = {a["resource_id"]: a["event_time"] for a in acts}
            for cat, cands in pool.items():
                needy = [(r, s) for r, s in order if not any(
                    classify_resource(a["resource_id"])[1] == cat and s.user_id in self.owner_obs.get(a["resource_id"], {})
                    for a in acts)]
                if cat == "USER_PROFILE":
                    for res, status in STATUS_RESULT.items():
                        cs = [r for r, _s in needy if r["reported_status"] == status]
                        rs = [rid for rid, rr in cands if rr == res]
                        self._assign(cs, rs, act_time, slack=timedelta(minutes=15))
                else:
                    self._assign([r for r, _s in needy], [rid for rid, _rr in cands], act_time,
                                 slack=timedelta(hours=3))

    def _assign(self, cases, rids, act_time, slack):
        """Greedy in-order matching; a resource may only go to a case whose report it precedes."""
        free = list(rids)
        for r in cases:
            limit = ts(r["reported_at"]) + slack
            for rid in free:
                if ts(act_time[rid]) <= limit:
                    self.activity_owner.setdefault(rid, set()).add(r["case_id"])
                    free.remove(rid)
                    break

    # ------------------------------------------------------------ per case
    def linked_resources(self, req: dict, subj: Subject) -> set:
        my_day = req["reported_at"][:10]
        other_days = self.user_report_days.get(subj.user_id, set()) - {my_day}
        res = set()
        for rid, owners in self.owner_obs.items():
            if subj.user_id not in owners:
                continue
            days = self.worker_days.get(rid, set())
            # a resource processed for the same person's *other* request belongs to that request
            if days and (days & other_days) and my_day not in days:
                continue
            if rid.startswith("account_") and days and my_day not in days:
                continue
            res.add(rid)
        res |= {rid for rid, cases in self.activity_owner.items() if req["case_id"] in cases}
        return {rid for rid in res if not self._belongs_to_earlier_request(rid, req, subj)}

    def _belongs_to_earlier_request(self, rid, req, subj) -> bool:
        """Evidence that predates this request but falls inside the same person's earlier request
        window was already audited there (e.g. an attachment found in February)."""
        caps = [o.captured_at for o in self.obs_by_res.get(rid, []) if o.captured_at <= req["audit_at"]]
        if not caps or max(caps) >= req["requested_at"]:
            return False
        last = max(caps)
        return any(r["case_id"] != req["case_id"] and r["requested_at"] < req["requested_at"]
                   and r["requested_at"] <= last <= r["audit_at"]
                   for r in self.requests_by_user.get(subj.user_id, []))

    def coverage_absence(self, rid: str, system: str, audit_at: str):
        """Latest capture <= audit_at that includes the system's data but not this record."""
        best = None
        for fid, systems in self.store.coverage.items():
            cap = self.store.manifest[fid]["captured_at"]
            if system in systems and cap <= audit_at and (best is None or cap > best[1]):
                best = (fid, cap)
        if best is None:
            return None
        return Observation(best[0], best[1], best[1], rid, system, "USER_PROFILE", ABSENT, set(), "coverage")

    def state_at(self, rid: str, audit_at: str):
        """Latest observation captured at/before audit_at; worst copy wins inside one capture."""
        cands = [o for o in self.obs_by_res.get(rid, []) if o.captured_at <= audit_at and o.observed_at <= audit_at]
        if not cands:
            return None
        latest_cap = max(o.captured_at for o in cands)
        same = [o for o in cands if o.captured_at == latest_cap]
        # latest fact per copy (primary table, each replica, log stream) ...
        per_copy = {}
        for o in sorted(same, key=lambda o: (o.observed_at, SEVERITY.get(o.state, 0))):
            per_copy[o.source] = o
        # ... and a stale copy anywhere still exposes the data, so the worst copy wins
        return max(per_copy.values(), key=lambda o: (SEVERITY.get(o.state, 0), o.observed_at))

    def category_of(self, rid: str, audit_at: str) -> str:
        cats = {o.category for o in self.obs_by_res.get(rid, []) if o.captured_at <= audit_at}
        for c in ("SUPPORT_ATTACHMENT", "ACCOUNT_TOMBSTONE", "SECONDARY_IDENTIFIER"):
            if c in cats:
                return c
        return classify_resource(rid)[1]

    def audit(self, req: dict, subj: Subject) -> tuple[str, list]:
        audit_at = req["audit_at"]
        findings, remediation, verification = [], [], []
        violations = False
        for rid in sorted(self.linked_resources(req, subj)):
            o = self.state_at(rid, audit_at)
            if o is None and rid.startswith("account_"):
                o = self.coverage_absence(rid, "account_service", audit_at)
                if o is None:   # no proof either way: still a required post-remediation check
                    verification.append(("VERIFY_RECORD_ABSENT", "account_service", rid, "", "post"))
                    continue
            if o is None:
                cat = classify_resource(rid)[1]
                if self.rules.get(cat) == "RETAIN" and cat != "BACKUP_ARCHIVE" and self.obs_by_res.get(rid):
                    verification.append(("VERIFY_RECORD_PRESENT", classify_resource(rid)[0], rid, "", "post"))
                continue
            cat = self.category_of(rid, audit_at)
            action = self.rules.get(cat, "DELETE")
            sysid = o.system_id
            ev = o.file_id
            st = o.state
            plan = decide(cat, action, st)
            if plan is None:
                continue
            kind, ops = plan
            if kind == "violation":
                violations = True
                finding, fix, check = ops
                findings.append((finding, sysid, rid, ev))
                remediation.append((fix, sysid, rid, ""))
                verification.append((check, sysid, rid, ev, "post"))
            else:
                verification.append((ops, sysid, rid, ev, "now"))
        if violations:
            # future checks: every remediated item, retained records, and the primary account
            post = [(op, s, r, "") for op, s, r, _e, when in verification
                    if when == "post" or op == "VERIFY_RECORD_PRESENT" or s == "account_service"]
            steps = findings + remediation + post
            status = "INCOMPLETE"
        else:
            steps = [(op, s, r, e) for op, s, r, e, when in verification if when == "now"]
            status = "COMPLETE"
        steps.sort(key=lambda x: (OP_ORDER.index(x[0]), x[1], x[2]))
        out = []
        for i, (op, s, r, e) in enumerate(steps, 1):
            out.append({"step_no": i, "phase": PHASE[op], "system_id": s, "resource_id": r,
                        "operation": op, "state": OP_STATE[op], "evidence_id": e})
        return status, out


def decide(category: str, action: str, state: str):
    """Map (policy action, observed state) to a violation triple or a compliant check."""
    if category == "DATA_FLOW" or action == "DISABLE":
        if state == ACTIVE:
            return "violation", ("REPORT_ACTIVE_DATA_FLOW", "DISABLE_DATA_FLOW", "VERIFY_DATA_FLOW_DISABLED")
        if state in (DISABLED, ABSENT):
            return "ok", "VERIFY_DATA_FLOW_DISABLED"
        return None
    if category == "BACKUP_ARCHIVE":
        if state == ACTIVE:
            return "violation", ("REPORT_RESTORE_RISK", "BLOCK_RESTORE", "VERIFY_RESTORE_BLOCKED")
        if state == BLOCKED:
            return "ok", "VERIFY_RESTORE_BLOCKED"
        return None
    if action == "RETAIN":
        return ("ok", "VERIFY_RECORD_PRESENT") if state != ABSENT else None
    if action == "DELETE":
        if state in (PRESENT, IDENTIFIABLE, REDACTED, ANONYMIZED):
            return "violation", ("REPORT_REMAINING_RECORD", "DELETE_RECORD", "VERIFY_RECORD_ABSENT")
        return "ok", "VERIFY_RECORD_ABSENT"
    if action == "REDACT":
        if state == IDENTIFIABLE:
            return "violation", ("REPORT_EXPOSED_PII", "REDACT_RECORD", "VERIFY_RECORD_REDACTED")
        if state == PRESENT:   # bare copy of a contact record (e.g. legacy index) -> remove it
            return "violation", ("REPORT_REMAINING_RECORD", "DELETE_RECORD", "VERIFY_RECORD_ABSENT")
        if state == REDACTED:
            return "ok", "VERIFY_RECORD_REDACTED"
        return None
    if action == "ANONYMIZE":
        if state == IDENTIFIABLE:
            return "violation", ("REPORT_EXPOSED_PII", "ANONYMIZE_RECORD", "VERIFY_RECORD_ANONYMIZED")
        if state == PRESENT:
            return "violation", ("REPORT_REMAINING_RECORD", "DELETE_RECORD", "VERIFY_RECORD_ABSENT")
        if state == ANONYMIZED:
            return "ok", "VERIFY_RECORD_ANONYMIZED"
        return None
    return None


def _num(s: str) -> int:
    digits = "".join(ch for ch in s if ch.isdigit())
    return int(digits) if digits else 0


def _keys(text: str):
    from .model import keys_from_text
    return keys_from_text(text)
