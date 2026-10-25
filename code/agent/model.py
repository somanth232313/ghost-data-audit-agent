"""Shared data model: every evidence source is normalised into Observations."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# Resource-id prefix -> (system_id, data_category). Longest prefix wins.
PREFIXES = [
    ("audit_record_", "security_service", "SECURITY_AUDIT_LOG"),
    ("data_flow_", "integration_service", "DATA_FLOW"),
    ("email_delivery_", "email_delivery_service", "EMAIL_DELIVERY_LOG"),
    ("legal_hold_", "legal_service", "LEGAL_HOLD_RECORD"),
    ("tombstone_", "identity_service", "ACCOUNT_TOMBSTONE"),
    ("analytics_", "analytics_platform", "ANALYTICS_PROFILE"),
    ("marketing_", "marketing_platform", "MARKETING_PROFILE"),
    ("telemetry_", "telemetry_service", "PRODUCT_TELEMETRY"),
    ("account_", "account_service", "USER_PROFILE"),
    ("search_", "search_service", "SEARCH_INDEX_ENTRY"),
    ("support_", "support_platform", "SUPPORT_MESSAGE"),
    ("export_", "export_service", "USER_EXPORT"),
    ("backup_", "backup_service", "BACKUP_ARCHIVE"),
    ("object_", "object_storage", "SUPPORT_ATTACHMENT"),
    ("cache_", "cache_service", "CACHE_ENTRY"),
]

RESOURCE_RE = re.compile(
    r"\b(?:audit_record|data_flow|email_delivery|legal_hold|tombstone|analytics|marketing|"
    r"telemetry|account|search|support|export|backup|object|cache)_\d+\b"
)
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
USER_RE = re.compile(r"\buser_\d+\b")
HASH_RE = re.compile(r"\b[0-9a-f]{64}\b")

# Record states used internally.
PRESENT, ABSENT = "PRESENT", "ABSENT"
IDENTIFIABLE, REDACTED, ANONYMIZED = "IDENTIFIABLE", "REDACTED", "ANONYMIZED"
ACTIVE, DISABLED, BLOCKED = "ACTIVE", "DISABLED", "BLOCKED"


def classify_resource(resource_id: str) -> tuple[str, str]:
    for prefix, system, category in PREFIXES:
        if resource_id.startswith(prefix):
            return system, category
    return "unknown_service", "UNKNOWN"


def norm_phone(value: str) -> str:
    digits = re.sub(r"\D", "", value or "")
    return digits if len(digits) >= 7 else ""


def keys_from_text(text) -> set[str]:
    """Extract every linkable identifier from an arbitrary value."""
    if text is None:
        return set()
    if isinstance(text, (dict, list)):
        out: set[str] = set()
        items = text.values() if isinstance(text, dict) else text
        for v in items:
            out |= keys_from_text(v)
        return out
    s = str(text)
    out = {e.lower() for e in EMAIL_RE.findall(s)}
    out |= set(USER_RE.findall(s))
    out |= set(HASH_RE.findall(s.lower()))
    if s.strip().startswith("+") or re.fullmatch(r"[\d\s()+-]{7,}", s.strip()):
        p = norm_phone(s)
        if p:
            out.add("tel:" + p)
    return out


@dataclass
class Observation:
    file_id: str            # evidence_NN (or activity id)
    captured_at: str        # capture time of the evidence file
    observed_at: str        # time the fact was true
    resource_id: str
    system_id: str
    category: str
    state: str              # PRESENT / ABSENT / IDENTIFIABLE / REDACTED / ...
    keys: set = field(default_factory=set)   # identifiers linking it to a subject
    source: str = ""        # e.g. "primary", "failover_replica", "log", "image"
    note: str = ""

    def __post_init__(self):
        if not self.system_id or self.system_id == "unknown_service":
            sys_, cat = classify_resource(self.resource_id)
            self.system_id = sys_
            if not self.category:
                self.category = cat
        if not self.category:
            self.category = classify_resource(self.resource_id)[1]
