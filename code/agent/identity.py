"""Identity resolution: current + historical identifiers for every user."""
from __future__ import annotations

import csv
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .model import norm_phone


@dataclass
class Subject:
    user_id: str
    name: str = ""
    emails: set = field(default_factory=set)
    phones: set = field(default_factory=set)       # "tel:<digits>"
    hashes: set = field(default_factory=set)
    audit_ids: list = field(default_factory=list)  # audit_ids of identity changes

    @property
    def keys(self) -> set:
        return {self.user_id} | self.emails | self.phones | self.hashes


class IdentityIndex:
    def __init__(self, dataset: Path):
        self.subjects: dict[str, Subject] = {}
        self.key_to_users: dict[str, set] = defaultdict(set)
        for r in csv.DictReader(open(dataset / "user_identities.csv")):
            s = Subject(r["user_id"], r["name"])
            s.emails.add(r["email"].lower())
            if norm_phone(r["phone"]):
                s.phones.add("tel:" + norm_phone(r["phone"]))
            s.hashes.add(r["email_hash"].lower())
            self.subjects[s.user_id] = s
        for r in csv.DictReader(open(dataset / "user_audit_log.csv")):
            s = self.subjects.setdefault(r["user_id"], Subject(r["user_id"]))
            s.audit_ids.append(r["audit_id"])
            for v in (r["old_value"], r["new_value"]):
                if r["field_name"] == "email":
                    s.emails.add(v.lower())
                elif r["field_name"] == "phone" and norm_phone(v):
                    s.phones.add("tel:" + norm_phone(v))
                elif r["field_name"] == "email_hash":
                    s.hashes.add(v.lower())
        for s in self.subjects.values():
            for k in s.keys:
                self.key_to_users[k].add(s.user_id)

    def resolve(self, email: str) -> Subject | None:
        users = self.key_to_users.get(email.strip().lower(), set())
        if not users:
            return None
        # prefer the user whose *current* email matches; else any historical owner
        return self.subjects[sorted(users)[0]]

    def owners(self, keys: set) -> set:
        out = set()
        for k in keys:
            out |= self.key_to_users.get(k, set())
        return out
