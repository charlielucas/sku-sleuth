import json
import sys

sys.path.insert(0, "scripts")
from update_golden import GOLDEN_PATH, compute_gold_outcomes  # noqa: E402


def test_gold_outcomes_match_snapshot():
    snapshot = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    current = compute_gold_outcomes()
    assert set(snapshot) == set(
        current
    ), "gold row_id set changed — rerun scripts/update_golden.py deliberately"
    diffs = [
        f"{rid}: {snapshot[rid]} -> {current[rid]}"
        for rid in sorted(snapshot)
        if snapshot[rid] != current[rid]
    ]
    assert not diffs, "gold outcomes drifted:\n" + "\n".join(diffs)
