#!/usr/bin/env python3
"""
AI Data Cleanup Evaluation Harness
-----------------------------------
Measures how well an LLM handles three real-estate data tasks:
  1. Address standardization
  2. Duplicate detection
  3. Structured extraction from free-text remarks

Scores every result against hand-labeled ground truth and prints
an accuracy report with failure examples.

Usage:  python3 eval.py
"""

import csv
import json
import re
import time
from pathlib import Path

# ── Normalization helpers ──

_ADDR_ABBREV = {
    "street": "st", "str": "st",
    "avenue": "ave", "av": "ave",
    "boulevard": "blvd",
    "road": "rd",
    "drive": "dr",
    "lane": "ln",
    "court": "ct",
    "circle": "cir",
    "place": "pl",
    "way": "way",
    "highway": "hwy",
    "parkway": "pkwy",
    "terrace": "ter",
    "suite": "ste",
    "apartment": "apt",
    "north": "n", "south": "s", "east": "e", "west": "w",
    "northeast": "ne", "northwest": "nw", "southeast": "se", "southwest": "sw",
}

def normalize_address(s):
    if s is None:
        return ""
    s = s.lower()
    s = re.sub(r"[^\w\s]", " ", s)
    tokens = s.split()
    tokens = [_ADDR_ABBREV.get(t, t) for t in tokens]
    return " ".join(tokens)

def coerce_number(v):
    """Return float(v) or None if not numeric. Strips $, commas."""
    if v is None:
        return None
    s = str(v).strip().replace("$", "").replace(",", "")
    if s == "":
        return None
    try:
        return float(s)
    except ValueError:
        return None

def coerce_bool(v):
    """Return True/False/None. Accepts bools, 'true'/'false', 'yes'/'no', 1/0."""
    if isinstance(v, bool):
        return v
    if v is None:
        return None
    s = str(v).strip().lower()
    if s in ("true", "yes", "y", "1"):
        return True
    if s in ("false", "no", "n", "0"):
        return False
    return None

DATA_DIR = Path(__file__).parent
MESSY_CSV = DATA_DIR / "messy_data.csv"
TRUTH_CSV = DATA_DIR / "ground_truth.csv"
OUTPUTS_DIR = DATA_DIR / "outputs"

def load_messy():
    with open(MESSY_CSV) as f:
        return list(csv.DictReader(f))

def load_truth():
    truth = {}
    with open(TRUTH_CSV) as f:
        for row in csv.DictReader(f):
            truth[row["id"]] = row
    return truth

# ── Load LLM outputs ──

def load_address_results():
    with open(OUTPUTS_DIR / "task1_addresses.json") as f:
        return {r["id"]: r["canonical_address"] for r in json.load(f)}

def load_dedup_results(expected_ids=None):
    """Return {id: canonical_id_or_None}. Every id that appears in any group
    is recorded; canonical members map to None. If expected_ids is provided,
    ids absent from the LLM output are returned as the sentinel "<missing>"
    so the scorer can distinguish "predicted unique" from "forgotten"."""
    with open(OUTPUTS_DIR / "task2_dedup.json") as f:
        groups = json.load(f)
    mapping = {}
    for group in groups:
        group = sorted(group, key=int)
        canonical = group[0]
        for member in group:
            mapping[member] = None if member == canonical else canonical
    if expected_ids is not None:
        for rid in expected_ids:
            if rid not in mapping:
                mapping[rid] = "<missing>"
    return mapping

def load_extraction_results():
    with open(OUTPUTS_DIR / "task3_extraction.json") as f:
        return {r["id"]: r for r in json.load(f)}

# ── Scoring ──

def score_addresses(predictions, truth):
    correct, total, failures = 0, 0, []
    for rid, trow in truth.items():
        expected = trow["canonical_address"]
        total += 1
        predicted = predictions.get(rid)
        if predicted is None:
            failures.append({"id": rid, "expected": expected, "got": "<missing>"})
            continue
        if normalize_address(predicted) == normalize_address(expected):
            correct += 1
        else:
            failures.append({"id": rid, "expected": expected, "got": predicted})
    spurious = sorted(set(predictions) - set(truth))
    if spurious:
        failures.append({"spurious_ids": spurious})
    return correct, total, failures

def score_dedup(predictions, truth):
    correct, total, failures = 0, 0, []
    for rid, trow in truth.items():
        expected_dup = trow["duplicate_of"].strip() or None
        predicted_dup = predictions.get(rid, "<missing>")
        total += 1
        if expected_dup == predicted_dup:
            correct += 1
        else:
            failures.append({"id": rid, "expected_dup_of": expected_dup, "predicted_dup_of": predicted_dup})
    spurious = sorted(k for k in predictions if k not in truth)
    if spurious:
        failures.append({"spurious_ids": spurious})
    return correct, total, failures

EXTRACTION_FIELDS = ["beds", "baths", "hoa_monthly", "year_built", "has_pool", "has_garage"]
NUMERIC_FIELDS = {"beds", "baths", "hoa_monthly", "year_built"}
BOOLEAN_FIELDS = {"has_pool", "has_garage"}

def _field_match(field, expected_raw, predicted_raw):
    if field in NUMERIC_FIELDS:
        ev, pv = coerce_number(expected_raw), coerce_number(predicted_raw)
        return ev is not None and pv is not None and ev == pv
    if field in BOOLEAN_FIELDS:
        ev, pv = coerce_bool(expected_raw), coerce_bool(predicted_raw)
        return ev is not None and pv is not None and ev == pv
    return str(expected_raw).strip().lower() == str(predicted_raw).strip().lower()

def score_extraction(predictions, truth):
    correct, total, failures = 0, 0, []
    for rid, expected in truth.items():
        pred = predictions.get(rid)
        for f in EXTRACTION_FIELDS:
            total += 1
            ev_raw = expected[f]
            if pred is None:
                failures.append({"id": rid, "field": f, "expected": ev_raw, "got": "<missing>"})
                continue
            pv_raw = pred.get(f)
            if _field_match(f, ev_raw, pv_raw):
                correct += 1
            else:
                failures.append({"id": rid, "field": f, "expected": ev_raw, "got": pv_raw})
    spurious = sorted(set(predictions) - set(truth))
    if spurious:
        failures.append({"spurious_ids": spurious})
    return correct, total, failures

# ── Report ──

def print_report(task_name, correct, total, failures, max_failures=5):
    pct = (correct / total * 100) if total else 0
    print(f"\n{'='*60}")
    print(f"  {task_name}")
    print(f"{'='*60}")
    print(f"  Accuracy: {correct}/{total} ({pct:.1f}%)")
    if failures:
        print(f"  Errors:   {len(failures)}")
        print(f"  Sample failures:")
        for f in failures[:max_failures]:
            print(f"    - {f}")
    else:
        print(f"  No errors!")
    return pct

# ── Main ──

def main():
    print()
    print("  AI Data Cleanup Evaluation Harness")
    print("  Constellation Data Labs — Interview Demo")
    print("=" * 60)
    print()

    rows = load_messy()
    truth = load_truth()
    print(f"  Dataset:  {len(rows)} messy records from {len(set(r['source'] for r in rows))} MLS sources")
    print(f"  Truth:    {len(truth)} hand-labeled ground-truth rows")
    print(f"  Model:    Claude (Anthropic)")
    print()

    results = {}

    # Task 1
    print("  Running Task 1: Address Standardization...")
    addr_pred = load_address_results()
    c, t, f = score_addresses(addr_pred, truth)
    results["Address Standardization"] = print_report("TASK 1: Address Standardization", c, t, f)

    # Task 2
    print("\n  Running Task 2: Duplicate Detection...")
    dedup_pred = load_dedup_results(expected_ids=set(truth))
    c, t, f = score_dedup(dedup_pred, truth)
    results["Duplicate Detection"] = print_report("TASK 2: Duplicate Detection", c, t, f)

    # Task 3
    print("\n  Running Task 3: Structured Extraction...")
    extract_pred = load_extraction_results()
    c, t, f = score_extraction(extract_pred, truth)
    results["Structured Extraction"] = print_report("TASK 3: Structured Extraction", c, t, f)

    # Summary
    print(f"\n{'='*60}")
    print(f"  SUMMARY")
    print(f"{'='*60}")
    for task, pct in results.items():
        bar = "█" * int(pct // 5) + "░" * (20 - int(pct // 5))
        print(f"  {task:<30s} {bar} {pct:.1f}%")

    print(f"\n  Records processed:  {len(rows)}")
    print(f"  Estimated API cost per 1M records:  ~$300-1000 (batched)")
    print(f"  Estimated time per 1M records:      ~8-12 hours (batched)")

    # Verdict
    print(f"\n{'='*60}")
    print(f"  VERDICT: Where to use AI, where not to")
    print(f"{'='*60}")
    for task, pct in results.items():
        if pct >= 95:
            print(f"  ✓ {task}: AUTOMATE — accuracy high enough for production with spot-checks")
        elif pct >= 80:
            print(f"  ~ {task}: HUMAN-IN-LOOP — use AI as first pass, flag low-confidence for review")
        else:
            print(f"  ✗ {task}: DON'T TRUST — error rate too high for a data product")
    print()

if __name__ == "__main__":
    main()
