"""Team-authored evaluation cases.

Each case builds a tiny synthetic dataset (identities, activity, rules, evidence) in a
temporary folder, runs the real pipeline on it (deterministic mode) and compares the
output with the answer we expect from the problem statement's rules.

    python3 code/evaluation/custom_cases.py
"""
from __future__ import annotations

import csv
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RULES = ROOT / "dataset" / "deletion_rules.csv"

IDENT_HDR = ["user_id", "name", "email", "phone", "email_hash", "updated_at"]
AUDIT_HDR = ["audit_id", "user_id", "event_time", "field_name", "old_value", "new_value"]
ACT_HDR = ["activity_id", "event_time", "event_type", "source_system", "target_system", "resource_id",
           "reported_result", "details"]
REQ_HDR = ["case_id", "email", "requested_at", "reported_status", "reported_at", "audit_at", "request_text"]
H1 = "a" * 64
H2 = "b" * 64

USERS = [
    ["user_901", "Rhea Varma", "rhea.varma@example.org", "+91 90000 11111", H1, "2026-05-01T00:00:00Z"],
    ["user_902", "Tomas Lind", "tomas.lind@example.org", "+46 70 000 2222", H2, "2026-05-01T00:00:00Z"],
    ["user_903", "Tomas Lindqvist", "tomas.lindqvist@example.org", "+46 70 000 3333", "c" * 64,
     "2026-05-01T00:00:00Z"],
]
AUDIT = [["audit_9001", "user_901", "2026-04-01T00:00:00Z", "email", "rhea.old@example.org", "rhea.varma@example.org"]]
DELETE_TEXT = "Please delete my account and every copy of my personal data."


def inventory_sql(captured: str, rows: list[str]) -> str:
    return "\n".join([
        "BEGIN TRANSACTION;",
        "CREATE TABLE snapshot_scope (origin_system TEXT PRIMARY KEY, captured_at TEXT NOT NULL);",
        "CREATE TABLE customer_profiles (resource_id TEXT PRIMARY KEY, customer_reference TEXT, full_name TEXT,"
        " email TEXT, phone TEXT, email_hash TEXT, account_status TEXT NOT NULL, updated_at TEXT NOT NULL);",
        "CREATE TABLE customer_search_documents (resource_id TEXT PRIMARY KEY, customer_reference TEXT,"
        " display_name TEXT, email TEXT, phone TEXT, email_hash TEXT, index_name TEXT NOT NULL, updated_at TEXT NOT NULL);",
        "CREATE TABLE connector_subscriptions (resource_id TEXT PRIMARY KEY, customer_reference TEXT,"
        " customer_email TEXT, connector_name TEXT NOT NULL, enabled INTEGER NOT NULL, last_delivery_status TEXT"
        " NOT NULL, updated_at TEXT NOT NULL);",
        "CREATE TABLE backup_restore_entries (resource_id TEXT PRIMARY KEY, customer_reference TEXT,"
        " customer_email TEXT, archive_key TEXT NOT NULL, restore_allowed INTEGER NOT NULL, updated_at TEXT NOT NULL);",
        "CREATE TABLE security_audit_records (resource_id TEXT PRIMARY KEY, subject_reference TEXT NOT NULL,"
        " event_code TEXT NOT NULL, retention_until TEXT NOT NULL, updated_at TEXT NOT NULL);",
        f"INSERT INTO snapshot_scope VALUES ('account_service', '{captured}');",
        *rows, "COMMIT;"])


def step(n, phase, sysid, rid, op, state, ev=""):
    return {"step_no": n, "phase": phase, "system_id": sysid, "resource_id": rid,
            "operation": op, "state": state, "evidence_id": ev}


CASES = [
    {
        "name": "Historical email + search entry under the old address",
        "why": "Request uses the CURRENT email, but the index still holds the OLD one (from user_audit_log).",
        "request": ["case_c1", "rhea.varma@example.org", "2026-06-01T09:00:00Z", "COMPLETE",
                    "2026-06-02T12:00:00Z", "2026-06-10T12:00:00Z", DELETE_TEXT],
        "activity": [["activity_c1", "2026-06-02T10:00:00Z", "DELETE_RECORD", "deletion_worker", "account_service",
                      "account_9901", "SUCCESS", "Deletion worker submitted primary profile account_9901."]],
        "evidence": [("evidence_c1", "inv.sql", "2026-06-10T08:00:00Z", inventory_sql("2026-06-10T08:00:00Z", [
            "INSERT INTO customer_search_documents VALUES ('search_9901', NULL, NULL, 'rhea.old@example.org',"
            " NULL, NULL, 'customer_directory_v3', '2026-06-09T00:00:00Z');"]))],
        "expect": ("INCOMPLETE", [
            step(1, "FINDING", "search_service", "search_9901", "REPORT_REMAINING_RECORD", "PRESENT", "evidence_c1"),
            step(2, "REMEDIATION", "search_service", "search_9901", "DELETE_RECORD", "ABSENT"),
            step(3, "VERIFICATION", "account_service", "account_9901", "VERIFY_RECORD_ABSENT", "ABSENT"),
            step(4, "VERIFICATION", "search_service", "search_9901", "VERIFY_RECORD_ABSENT", "ABSENT")]),
    },
    {
        "name": "Evidence captured after audit_at must be ignored",
        "why": "The only capture showing the leftover record is dated after the audit cutoff.",
        "request": ["case_c2", "rhea.varma@example.org", "2026-06-01T09:00:00Z", "COMPLETE",
                    "2026-06-02T12:00:00Z", "2026-06-05T12:00:00Z", DELETE_TEXT],
        "activity": [],
        "evidence": [("evidence_c2", "late.sql", "2026-06-20T08:00:00Z", inventory_sql("2026-06-20T08:00:00Z", [
            "INSERT INTO customer_profiles VALUES ('account_9902', 'user_901', 'Rhea Varma', 'rhea.varma@example.org',"
            " NULL, NULL, 'ACTIVE', '2026-06-19T00:00:00Z');"]))],
        "expect": ("COMPLETE", []),
    },
    {
        "name": "Active sync + restorable backup: disable/block before deleting",
        "why": "Remediation must stop recreation paths first; verification covers every remediated item and "
               "the retained security audit record.",
        "request": ["case_c3", "tomas.lind@example.org", "2026-06-01T09:00:00Z", "PARTIALLY_COMPLETE",
                    "2026-06-02T12:00:00Z", "2026-06-10T12:00:00Z", DELETE_TEXT],
        "activity": [],
        "evidence": [("evidence_c3", "inv.sql", "2026-06-09T08:00:00Z", inventory_sql("2026-06-09T08:00:00Z", [
            "INSERT INTO customer_profiles VALUES ('account_9903', 'user_902', 'Tomas Lind', 'tomas.lind@example.org',"
            " NULL, NULL, 'DELETION_PENDING', '2026-06-08T00:00:00Z');",
            "INSERT INTO connector_subscriptions VALUES ('data_flow_9903', 'user_902', NULL, 'customer_events_connector',"
            " 1, 'delivered', '2026-06-08T00:00:00Z');",
            "INSERT INTO backup_restore_entries VALUES ('backup_9903', NULL, 'tomas.lind@example.org',"
            " 'archive/x.tar.zst', 1, '2026-06-08T00:00:00Z');",
            "INSERT INTO security_audit_records VALUES ('audit_record_9903', 'user_902', 'ACCOUNT_DELETION_RECORDED',"
            " '2031-12-31T23:59:59Z', '2026-06-08T00:00:00Z');",
            # decoy: similar name / email that belongs to another user
            "INSERT INTO customer_search_documents VALUES ('search_9909', NULL, NULL, 'tomas.lindqvist@example.org',"
            " NULL, NULL, 'customer_directory_v3', '2026-06-08T00:00:00Z');"]))],
        "expect": ("INCOMPLETE", [
            step(1, "FINDING", "account_service", "account_9903", "REPORT_REMAINING_RECORD", "PRESENT", "evidence_c3"),
            step(2, "FINDING", "integration_service", "data_flow_9903", "REPORT_ACTIVE_DATA_FLOW", "ACTIVE", "evidence_c3"),
            step(3, "FINDING", "backup_service", "backup_9903", "REPORT_RESTORE_RISK", "ACTIVE", "evidence_c3"),
            step(4, "REMEDIATION", "integration_service", "data_flow_9903", "DISABLE_DATA_FLOW", "DISABLED"),
            step(5, "REMEDIATION", "backup_service", "backup_9903", "BLOCK_RESTORE", "BLOCKED"),
            step(6, "REMEDIATION", "account_service", "account_9903", "DELETE_RECORD", "ABSENT"),
            step(7, "VERIFICATION", "account_service", "account_9903", "VERIFY_RECORD_ABSENT", "ABSENT"),
            step(8, "VERIFICATION", "security_service", "audit_record_9903", "VERIFY_RECORD_PRESENT", "PRESENT"),
            step(9, "VERIFICATION", "integration_service", "data_flow_9903", "VERIFY_DATA_FLOW_DISABLED", "DISABLED"),
            step(10, "VERIFICATION", "backup_service", "backup_9903", "VERIFY_RESTORE_BLOCKED", "BLOCKED")]),
    },
    {
        "name": "Replication drift: primary deleted, failover replica still holds the profile",
        "why": "A stale replica copy is still a remaining record even though the primary is clean.",
        "request": ["case_c4", "tomas.lind@example.org", "2026-06-01T09:00:00Z", "COMPLETE",
                    "2026-06-02T12:00:00Z", "2026-06-10T12:00:00Z", DELETE_TEXT],
        "activity": [],
        "evidence": [("evidence_c4", "replica.sql", "2026-06-09T08:00:00Z", "\n".join([
            "BEGIN TRANSACTION;",
            "CREATE TABLE customer_profiles_primary (resource_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, full_name TEXT,"
            " email TEXT, phone TEXT, account_status TEXT, updated_at TEXT);",
            "CREATE TABLE customer_profiles_failover_replica (resource_id TEXT PRIMARY KEY, user_id TEXT NOT NULL,"
            " full_name TEXT, email TEXT, phone TEXT, account_status TEXT, updated_at TEXT);",
            "INSERT INTO customer_profiles_failover_replica VALUES ('account_9904','user_902','Tomas Lind',"
            "'tomas.lind@example.org','+46 70 000 2222','ACTIVE','2026-06-08T00:00:00Z');",
            "COMMIT;"]))],
        "expect": ("INCOMPLETE", [
            step(1, "FINDING", "account_service", "account_9904", "REPORT_REMAINING_RECORD", "PRESENT", "evidence_c4"),
            step(2, "REMEDIATION", "account_service", "account_9904", "DELETE_RECORD", "ABSENT"),
            step(3, "VERIFICATION", "account_service", "account_9904", "VERIFY_RECORD_ABSENT", "ABSENT")]),
    },
    {
        "name": "Non-deletion request (data export) is NOT_APPLICABLE",
        "why": "Even with leftover data in evidence, no deletion was authorised.",
        "request": ["case_c5", "tomas.lind@example.org", "2026-06-01T09:00:00Z", "NOT_STARTED",
                    "2026-06-02T12:00:00Z", "2026-06-10T12:00:00Z",
                    "I'd like to download my account information in a portable format. Please keep the account as it is."],
        "activity": [],
        "evidence": [],
        "expect": ("NOT_APPLICABLE", []),
    },
]


def write_csv(path, header, rows):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)


def run_case(case) -> tuple[str, list]:
    tmp = Path(tempfile.mkdtemp(prefix="ghost_case_"))
    try:
        ds = tmp / "dataset"
        (ds / "evidence" / "x").mkdir(parents=True)
        write_csv(ds / "user_identities.csv", IDENT_HDR, USERS)
        write_csv(ds / "user_audit_log.csv", AUDIT_HDR, AUDIT)
        write_csv(ds / "system_activity.csv", ACT_HDR, case["activity"])
        write_csv(ds / "deletion_requests.csv", REQ_HDR, [case["request"]])
        write_csv(ds / "sample_deletion_requests.csv", REQ_HDR, [])
        shutil.copy(RULES, ds / "deletion_rules.csv")
        manifest = []
        for fid, name, cap, body in case["evidence"]:
            (ds / "evidence" / "x" / name).write_text(body)
            manifest.append([fid, f"evidence/x/{name}", "custom", cap])
        write_csv(ds / "evidence_files.csv", ["file_id", "file_path", "origin", "captured_at"], manifest)
        out = tmp / "out.csv"
        subprocess.run([sys.executable, str(ROOT / "code" / "main.py"), "--no-llm", "--dataset", str(ds),
                        "--output", str(out)], check=True, capture_output=True)
        row = next(csv.DictReader(open(out)))
        return row["deletion_status"], json.loads(row["steps_json"])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    passed = 0
    for c in CASES:
        status, steps = run_case(c)
        ok = (status, steps) == c["expect"]
        passed += ok
        print(f"[{'PASS' if ok else 'FAIL'}] {c['name']}")
        if not ok:
            print("   expected:", c["expect"][0], json.dumps(c["expect"][1]))
            print("   got     :", status, json.dumps(steps))
    print(f"{passed}/{len(CASES)} custom cases passed")
    return passed == len(CASES)


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
