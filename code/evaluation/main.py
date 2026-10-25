"""Score predictions against labelled answers (default: the 32 public samples).

    python3 code/evaluation/main.py --pred sample_pred.csv --gold dataset/sample_output.csv [-v]

Metrics
  status accuracy   - deletion_status exact match per case
  step P/R/F1       - steps matched as (phase, system, resource, operation, state, evidence)
  field accuracy    - per-field agreement on position-aligned steps
  sequence accuracy - whole steps_json array identical (order included)
"""
from __future__ import annotations

import argparse
import csv
import json
from collections import Counter

FIELDS = ("phase", "system_id", "resource_id", "operation", "state", "evidence_id")


def load(path):
    out = {}
    for r in csv.DictReader(open(path, encoding="utf-8")):
        out[r["case_id"]] = (r["deletion_status"], json.loads(r["steps_json"] or "[]"))
    return out


def key(step, with_evidence=True):
    f = FIELDS if with_evidence else FIELDS[:-1]
    return tuple(step.get(k, "") for k in f)


def score(pred: dict, gold: dict, verbose=False) -> dict:
    n = len(gold)
    status_ok = seq_ok = 0
    tp = fp = fn = tp_ne = 0
    field_hits, field_total = Counter(), 0
    confusion = Counter()
    failures = []
    for cid, (gs, gsteps) in sorted(gold.items()):
        ps, psteps = pred.get(cid, ("MISSING", []))
        confusion[(gs, ps)] += 1
        status_ok += ps == gs
        gset, pset = Counter(key(s) for s in gsteps), Counter(key(s) for s in psteps)
        inter = sum((gset & pset).values())
        tp += inter
        fp += sum(pset.values()) - inter
        fn += sum(gset.values()) - inter
        tp_ne += sum((Counter(key(s, False) for s in gsteps) & Counter(key(s, False) for s in psteps)).values())
        same_seq = [key(s) for s in gsteps] == [key(s) for s in psteps] and \
            all(s.get("step_no") == i for i, s in enumerate(psteps, 1))
        seq_ok += same_seq and ps == gs
        for i in range(max(len(gsteps), len(psteps))):
            field_total += 1
            g = gsteps[i] if i < len(gsteps) else {}
            p = psteps[i] if i < len(psteps) else {}
            for f in FIELDS:
                field_hits[f] += bool(g) and bool(p) and g.get(f) == p.get(f)
        if not same_seq or ps != gs:
            failures.append((cid, gs, ps, gset - pset, pset - gset))
    prec = tp / (tp + fp) if tp + fp else 1.0
    rec = tp / (tp + fn) if tp + fn else 1.0
    res = {
        "cases": n,
        "status_accuracy": status_ok / n,
        "step_precision": prec, "step_recall": rec,
        "step_f1": 2 * prec * rec / (prec + rec) if prec + rec else 0.0,
        "step_recall_ignoring_evidence": tp_ne / (tp + fn) if tp + fn else 1.0,
        "field_accuracy": {f: field_hits[f] / field_total if field_total else 1.0 for f in FIELDS},
        "sequence_accuracy": seq_ok / n,
        "confusion": {f"{g}->{p}": c for (g, p), c in sorted(confusion.items())},
    }
    if verbose:
        for cid, gs, ps, missing, extra in failures:
            print(f"--- {cid}: gold={gs} pred={ps}")
            for m in missing:
                print("   MISSING", m)
            for e in extra:
                print("   EXTRA  ", e)
    res["failures"] = [f[0] for f in failures]
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pred", required=True)
    ap.add_argument("--gold", default="dataset/sample_output.csv")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    res = score(load(a.pred), load(a.gold), a.verbose)
    print(json.dumps(res, indent=2))


if __name__ == "__main__":
    main()
