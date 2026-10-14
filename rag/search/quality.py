"""Conservative default selection across all modes, with paired complete metrics."""
import math

METRICS = ("evidence_recall@5", "evidence_recall@10", "all_evidence@5", "all_evidence@10")
MODES = ("dense", "bm25", "hybrid")
# Equal quality: prefer no embedding, then one retrieval branch, then two branches.
TIE_PRIORITY = ("bm25", "dense", "hybrid")
EPSILON = 1e-12


def default_mode_gate(reports, current_default="dense"):
    """Promote only a mode that dominates every candidate and improves the current mode.

    Four overall and eight category metrics are compared without scalar weights.
    Trade-offs and incomplete evidence retain the current default. NumPy is a
    safety baseline, not an available managed-search mode.
    """
    if current_default not in MODES:
        raise ValueError("invalid current default mode")
    reasons = []
    vectors, denominators = {}, {}
    for mode in ("numpy", *MODES):
        values, counts = [], []
        for label in ("overall", "policy", "technical"):
            for name in METRICS:
                try:
                    summary = reports[mode]
                    group = summary["overall"] if label == "overall" else summary["slices"]["category"][label]
                    metric = group["metrics"][name]
                    value, eligible, scored = metric["mean_scored"], metric["eligible_count"], metric["scored_count"]
                    valid = (type(value) in (float, int) and math.isfinite(value) and 0 <= value <= 1 and
                             type(eligible) is int and eligible > 0 and type(scored) is int and scored == eligible)
                except (KeyError, TypeError):
                    valid = False
                if not valid:
                    reasons.append(f"{mode}/{label}/{name}: incomplete or invalid")
                    continue
                values.append(value)
                counts.append(eligible)
        if len(values) == 12:
            vectors[mode], denominators[mode] = values, counts
            if any(counts[n] != counts[n + 4] + counts[n + 8] for n in range(4)):
                reasons.append(f"{mode}: category denominators do not sum to overall")
    if not reasons and any(counts != denominators["numpy"] for counts in denominators.values()):
        reasons.append("candidate denominators differ")
    result = {"policy_version": 2, "current_default": current_default, "passed": False,
              "recommended_default": current_default, "best_candidates": [], "reasons": reasons}
    if reasons:
        return result

    def no_worse(left, right):
        return all(a >= b - EPSILON for a, b in zip(vectors[left], vectors[right]))

    best = [mode for mode in MODES if all(no_worse(mode, other) for other in MODES)]
    result["best_candidates"] = best
    if not best:
        reasons.append("candidate metrics trade off; no mode dominates all candidates")
    elif current_default in best:
        reasons.append("current default is already among the best candidates")
    else:
        selected = next(mode for mode in TIE_PRIORITY if mode in best)
        if not no_worse(selected, "numpy"):
            reasons.append("best candidate regresses against the NumPy safety baseline")
        elif not any(a > b + EPSILON for a, b in zip(vectors[selected][:4], vectors[current_default][:4])):
            reasons.append("no strict overall improvement over current default")
        else:
            result.update(passed=True, recommended_default=selected)
    return result
