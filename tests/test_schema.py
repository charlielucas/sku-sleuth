from pathlib import Path

import pytest

from sku_sleuth.schema import SchemaContractError, inspect_raw_csv, validate_raw_csv


def write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8", newline="")
    return path


def test_schema_reports_no_drift_and_distinguishes_missing_from_empty(tmp_path):
    raw = write(tmp_path / "raw.csv", "row_id,sku,title\nR1,,A REAL TITLE\nR2,S\n")
    report = inspect_raw_csv(raw)
    assert report["drift_status"] == "none"
    assert report["drift_events"] == []
    assert report["missing_value_counts"] == {"row_id": 0, "sku": 0, "title": 1}
    assert report["null_value_counts"] == {"row_id": 0, "sku": 1, "title": 0}


def test_additive_field_is_explicit_nonbreaking_evidence(tmp_path):
    raw = write(tmp_path / "raw.csv", "title,source,row_id,sku\nA REAL TITLE,x,R1,S1\n")
    report = inspect_raw_csv(raw)
    assert report["passed"] is True
    assert report["drift_status"] == "additive"
    assert report["extra_fields"] == ["source"]
    assert report["drift_events"] == [{"kind": "additive_field", "field": "source"}]


@pytest.mark.parametrize(
    ("header", "placeholder"),
    [
        ("row_id,sku,title,", "__unnamed_column_4__"),
        ("row_id,sku,title,   ", "__unnamed_column_4__"),
    ],
)
def test_unnamed_header_field_is_breaking_strict_evidence(tmp_path, header, placeholder):
    raw = write(tmp_path / "raw.csv", f"{header}\nR1,S1,A REAL TITLE,extra\n")
    report = inspect_raw_csv(raw)

    assert report["passed"] is False
    assert report["failures"] == ["unnamed_header_fields"]
    assert report["drift_status"] == "breaking"
    assert report["observed_fields"] == ["row_id", "sku", "title", placeholder]
    assert report["extra_fields"] == [placeholder]
    assert report["drift_events"] == [{"kind": "unnamed_header_field", "field": placeholder}]
    assert report["effective_content_sha256"] is None


@pytest.mark.parametrize(
    ("body", "failure"),
    [
        ("row_id,sku\nR1,S1\n", "missing_required_fields"),
        ("row_id,sku,title\n", "empty_input"),
        ("row_id,sku,title\nR1,S1,ONE\nR1,S2,TWO\n", "duplicate_row_ids"),
        ("row_id,sku,title\nR1,S1\n", "missing_required_values"),
        ("row_id,sku,title,title\nR1,S1,ONE,TWO\n", "duplicate_header_fields"),
        ("row_id,sku,title\nR1,S1,ONE,EXTRA\n", "unlabeled_extra_values"),
    ],
)
def test_breaking_contract_writes_evidence_before_raising(tmp_path, body, failure):
    raw = write(tmp_path / "raw.csv", body)
    report_path = tmp_path / "schema_report.json"
    with pytest.raises(SchemaContractError, match=failure):
        validate_raw_csv(raw, report_path)
    assert report_path.exists()
    assert inspect_raw_csv(raw)["passed"] is False


def test_invalid_utf8_is_breaking_unknown_evidence(tmp_path):
    raw = tmp_path / "raw.csv"
    raw.write_bytes(b"\xff\xfe")
    report = inspect_raw_csv(raw)
    assert report["passed"] is False
    assert report["drift_status"] == "breaking"
    assert report["observed_fields"] == []
    assert report["drift_events"] == [{"kind": "invalid_utf8"}]


def test_empty_input_is_breaking_drift(tmp_path):
    raw = write(tmp_path / "raw.csv", "row_id,sku,title\n")
    report = inspect_raw_csv(raw)
    assert report["passed"] is False
    assert report["failures"] == ["empty_input"]
    assert report["drift_status"] == "breaking"
