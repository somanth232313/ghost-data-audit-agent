"""Ghost Data Still Exists - deletion audit agent (entry point).

    python3 code/main.py                       # audit dataset/deletion_requests.csv -> output.csv
    python3 code/main.py --input dataset/sample_deletion_requests.csv --output sample_pred.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent.engine import Auditor          # noqa: E402
from agent.evidence import EvidenceStore  # noqa: E402
from agent.identity import IdentityIndex  # noqa: E402
from agent.intent import classify_requests  # noqa: E402


def case_order(r: dict):
    """Natural order (case_2 < case_10), matching the order of the request files."""
    digits = "".join(ch for ch in r["case_id"] if ch.isdigit())
    return (int(digits) if digits else 0, r["case_id"])


def read_requests(path: Path) -> list[dict]:
    return list(csv.DictReader(open(path, encoding="utf-8")))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dataset", default=str(ROOT / "dataset"))
    ap.add_argument("--input", default=None, help="requests CSV (default: dataset/deletion_requests.csv)")
    ap.add_argument("--output", default=str(ROOT / "output.csv"))
    ap.add_argument("--no-llm", action="store_true", help="use only the deterministic fallbacks")
    a = ap.parse_args()

    dataset = Path(a.dataset)
    requests = read_requests(Path(a.input) if a.input else dataset / "deletion_requests.csv")
    # every request file shares one activity stream, so link resources using all of them
    universe = read_requests(dataset / "deletion_requests.csv") + read_requests(dataset / "sample_deletion_requests.csv")

    store = EvidenceStore(dataset).load_all()
    if not a.no_llm:
        from agent.vision import load_visual_evidence
        load_visual_evidence(store, csv.DictReader(open(dataset / "system_activity.csv", encoding="utf-8")))
    ids = IdentityIndex(dataset)
    auditor = Auditor(dataset, store, ids, universe)
    intents = classify_requests(requests, use_llm=not a.no_llm)

    rows = []
    for req in sorted(requests, key=case_order):
        if not intents[req["case_id"]]:
            rows.append((req["case_id"], "NOT_APPLICABLE", []))
            continue
        subj = ids.resolve(req["email"])
        if subj is None:
            rows.append((req["case_id"], "COMPLETE", []))
            continue
        status, steps = auditor.audit(req, subj)
        rows.append((req["case_id"], status, steps))

    with open(a.output, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["case_id", "deletion_status", "steps_json"])
        for cid, status, steps in rows:
            w.writerow([cid, status, json.dumps(steps, ensure_ascii=False, separators=(",", ":"))])
    print(f"wrote {len(rows)} rows to {a.output}")
    if not a.no_llm:
        write_usage_report(len(requests), Path(a.output).name)


def write_usage_report(n_requests: int, output_name: str):
    """Summarise every Gemini call behind this run (fresh calls + cache replays)."""
    import os
    from datetime import datetime, timezone
    from agent import llm
    usage = llm.Usage.calls
    rate_in = float(os.environ.get("GEMINI_PRICE_IN_PER_M", "0"))
    rate_out = float(os.environ.get("GEMINI_PRICE_OUT_PER_M", "0"))
    lines = [
        "# Token Usage and Cost Report", "",
        f"Generated automatically by `code/main.py` at {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} "
        f"for the run that wrote `{output_name}` ({n_requests} requests).", "",
        "Provider: **Google Gemini API** (REST `generateContent`, free tier). LLM work per run: request-intent "
        "classification (unique texts, batched 75 per call) and one vision extraction per screenshot/PDF. "
        "All evidence parsing, identity resolution, policy decisions and step ordering are deterministic code "
        "and use no tokens.", "",
        "| Model | API calls | Cache replays | Input tokens | Output tokens | Total tokens |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    tot = {"calls": 0, "cache_hits": 0, "input_tokens": 0, "output_tokens": 0}
    for model, c in sorted(usage.items()):
        lines.append(f"| {model} | {c['calls']} | {c['cache_hits']} | {c['input_tokens']:,} | "
                     f"{c['output_tokens']:,} | {c['input_tokens'] + c['output_tokens']:,} |")
        for k in tot:
            tot[k] += c[k]
    total_tokens = tot["input_tokens"] + tot["output_tokens"]
    lines.append(f"| **Overall** | **{tot['calls']}** | **{tot['cache_hits']}** | **{tot['input_tokens']:,}** | "
                 f"**{tot['output_tokens']:,}** | **{total_tokens:,}** |")
    cost = tot["input_tokens"] / 1e6 * rate_in + tot["output_tokens"] / 1e6 * rate_out
    per_req = total_tokens / n_requests if n_requests else 0
    lines += [
        "", "## Per-request averages", "",
        f"- Model calls (fresh + replayed) per request: {(tot['calls'] + tot['cache_hits']) / max(n_requests, 1):.3f}",
        f"- Average tokens per request: {per_req:,.1f}",
        f"- Average input / output tokens per request: {tot['input_tokens'] / max(n_requests, 1):,.1f} / "
        f"{tot['output_tokens'] / max(n_requests, 1):,.1f}",
        "", "## Cost", "",
        f"- Billed cost of this run: **${cost:.4f}** (free tier; rates used: ${rate_in}/M input, ${rate_out}/M output).",
        f"- Per request: ${cost / max(n_requests, 1):.6f}",
        "- To estimate paid-tier cost, set `GEMINI_PRICE_IN_PER_M` / `GEMINI_PRICE_OUT_PER_M` to the published "
        "rates for the model and re-run; the token counts above are exact values reported by the API "
        "(`usageMetadata`, thinking tokens counted as output).",
        "", "Token counts for cache replays are the counts recorded when the response was first generated, so "
        "the table reflects the full LLM work behind the output even when a re-run is served from cache.",
    ]
    (ROOT / "code" / "evaluation" / "usage_report.md").write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
