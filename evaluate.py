"""Scores the pipeline's output against ground_truth.csv. Never fed into the
pipeline itself -- this is purely for validating thresholds/weights/tiers,
which was the entire point of building the labeled dataset."""

import csv
import sys
from collections import defaultdict
from pipeline import run_pipeline


def load_ground_truth(path):
    gt = {}
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            gt[row["record_id"]] = row
    return gt


def main():
    txn_path = sys.argv[1] if len(sys.argv) > 1 else "transactions.csv"
    inv_path = sys.argv[2] if len(sys.argv) > 2 else "invoices.csv"
    gt_path = sys.argv[3] if len(sys.argv) > 3 else "ground_truth.csv"

    result = run_pipeline(txn_path, inv_path, llm_call_fn=None)
    outcomes = result["record_outcomes"]
    gt = load_ground_truth(gt_path)

    per_tag = defaultdict(lambda: {"correct": 0, "total": 0, "mismatches": []})
    overall_correct, overall_total = 0, 0

    for record_id, gt_row in gt.items():
        predicted = outcomes.get(record_id, {}).get("classification", "MISSING")
        expected = gt_row["true_classification"]
        tag = gt_row["edge_case_tag"]
        per_tag[tag]["total"] += 1
        overall_total += 1
        if predicted == expected:
            per_tag[tag]["correct"] += 1
            overall_correct += 1
        else:
            per_tag[tag]["mismatches"].append((record_id, expected, predicted))

    print("=" * 78)
    print(f"OVERALL ACCURACY vs GROUND TRUTH: {overall_correct}/{overall_total} "
          f"({100*overall_correct/overall_total:.1f}%)")
    print("=" * 78)
    print(f"{'Tag':<32}{'Correct/Total':<16}{'Accuracy'}")
    print("-" * 78)
    for tag, stats in sorted(per_tag.items()):
        acc = 100 * stats["correct"] / stats["total"]
        flag = "  <-- check" if acc < 100 else ""
        print(f"{tag:<32}{stats['correct']}/{stats['total']:<14}{acc:>6.1f}%{flag}")

    print("\n" + "=" * 78)
    print("MISMATCHES (expected -> predicted)")
    print("=" * 78)
    for tag, stats in sorted(per_tag.items()):
        for record_id, expected, predicted in stats["mismatches"]:
            reason = outcomes.get(record_id, {}).get("reason", "")
            print(f"[{tag}] {record_id}: expected {expected}, got {predicted}")
            print(f"    reason logged: {reason[:140]}")


if __name__ == "__main__":
    main()
