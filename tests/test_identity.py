import csv
import json
from pathlib import Path

from sku_sleuth.engine import decode_stage
from sku_sleuth.generate import generate, write_raw_csv
from sku_sleuth.models import Decode
from sku_sleuth.tiers.model import StubModel


def prepare_inputs(root: Path, *, reverse: bool = False) -> tuple[Path, Path, Path, Path]:
    root.mkdir()
    raw = root / "products.csv"
    write_raw_csv(generate(seed=42, rows=60), raw)
    if reverse:
        lines = raw.read_text(encoding="utf-8").splitlines()
        raw.write_text("\n".join([lines[0], *reversed(lines[1:])]) + "\n", encoding="utf-8")

    catalog = root / "catalog.csv"
    catalog_lines = Path("seeds/catalog.csv").read_text(encoding="utf-8").splitlines()
    if reverse:
        catalog_lines = [catalog_lines[0], *reversed(catalog_lines[1:])]
    catalog.write_text("\n".join(catalog_lines) + "\n", encoding="utf-8")

    brands = root / "brands.json"
    brand_data = json.loads(Path("seeds/brands.json").read_text(encoding="utf-8"))
    if reverse:
        for brand in brand_data:
            brand["aliases"] = list(reversed(brand["aliases"]))
    brands.write_text(
        json.dumps(list(reversed(brand_data)) if reverse else brand_data), encoding="utf-8"
    )

    fixture = root / "fixture.json"
    fixture_data = json.loads(Path("seeds/stub_model_fixture.json").read_text(encoding="utf-8"))
    items = list(fixture_data.items())
    fixture.write_text(
        json.dumps(dict(reversed(items)) if reverse else fixture_data), encoding="utf-8"
    )
    return raw, catalog, brands, fixture


def stage(root: Path, inputs: tuple[Path, Path, Path, Path], ruleset: str = "guarded") -> dict:
    raw, catalog, brands, fixture = inputs
    return decode_stage(
        raw,
        ruleset,
        StubModel.from_json(fixture),
        root,
        brands_path=brands,
        catalog_path=catalog,
    )


def test_recipe_identity_is_path_and_input_order_independent(tmp_path):
    left_inputs = prepare_inputs(tmp_path / "left-inputs")
    right_inputs = prepare_inputs(tmp_path / "right-inputs", reverse=True)
    left = stage(tmp_path / "left-run", left_inputs)
    right = stage(tmp_path / "right-run", right_inputs)
    assert left["batch_id"] == right["batch_id"]
    assert left["run_id"] == right["run_id"]


def test_display_only_source_path_is_absent_from_contractual_artifacts(tmp_path):
    left_inputs = prepare_inputs(tmp_path / "left-inputs")
    right_inputs = prepare_inputs(tmp_path / "right-inputs")
    renamed_raw = right_inputs[0].with_name("a-different-display-name.csv")
    right_inputs[0].rename(renamed_raw)
    right_inputs = (renamed_raw, *right_inputs[1:])
    left_dir, right_dir = tmp_path / "left-run", tmp_path / "right-run"
    left = stage(left_dir, left_inputs)
    right = stage(right_dir, right_inputs)
    left_schema = json.loads((left_dir / "schema_report.json").read_text(encoding="utf-8"))
    right_schema = json.loads((right_dir / "schema_report.json").read_text(encoding="utf-8"))
    assert "source" not in left and "source" not in right
    assert "source" not in left_schema and "source" not in right_schema
    assert left_schema == right_schema
    assert left == right


def test_recipe_identity_is_sensitive_to_each_effective_input(tmp_path):
    base_inputs = prepare_inputs(tmp_path / "base-inputs")
    base = stage(tmp_path / "base-run", base_inputs)

    raw_inputs = prepare_inputs(tmp_path / "raw-inputs")
    raw_lines = raw_inputs[0].read_text(encoding="utf-8").splitlines()
    raw_lines[1] += " CHANGED"
    raw_inputs[0].write_text("\n".join(raw_lines) + "\n", encoding="utf-8")

    catalog_inputs = prepare_inputs(tmp_path / "catalog-inputs")
    with open(catalog_inputs[1], "a", encoding="utf-8", newline="") as handle:
        handle.write("ZZZ-9999,Electrical,Spark Plugs,,,,,0\n")

    brand_inputs = prepare_inputs(tmp_path / "brand-inputs")
    brands = json.loads(brand_inputs[2].read_text(encoding="utf-8"))
    brands[0]["aliases"].append("EXTRA_ALIAS")
    brand_inputs[2].write_text(json.dumps(brands), encoding="utf-8")

    model_inputs = prepare_inputs(tmp_path / "model-inputs")
    fixture = json.loads(model_inputs[3].read_text(encoding="utf-8"))
    fixture["UNUSED FIXTURE KEY"] = {
        "category": "Electrical",
        "subcategory": "Spark Plugs",
        "attributes": {},
        "is_winter_rated": False,
    }
    model_inputs[3].write_text(json.dumps(fixture), encoding="utf-8")

    variants = [
        stage(tmp_path / "raw-run", raw_inputs),
        stage(tmp_path / "catalog-run", catalog_inputs),
        stage(tmp_path / "brand-run", brand_inputs),
        stage(tmp_path / "model-run", model_inputs),
        stage(tmp_path / "rules-run", base_inputs, ruleset="naive"),
    ]
    assert all(variant["batch_id"] != base["batch_id"] for variant in variants)


class VariableModel:
    name = "model"

    def __init__(self, category: str, subcategory: str):
        self.category = category
        self.subcategory = subcategory

    def identity(self) -> dict:
        return {"adapter": "external-model", "model_id": "same", "deterministic": False}

    def decode(self, product):
        return Decode(
            row_id=product.row_id,
            sku=product.sku,
            category=self.category,
            subcategory=self.subcategory,
            attributes={},
            is_winter_rated=False,
            tier=self.name,
            evidence="model:nondeterministic",
        )


def test_nondeterministic_outputs_share_recipe_but_get_distinct_run_ids(tmp_path):
    raw = tmp_path / "raw.csv"
    with open(raw, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(["row_id", "sku", "title"])
        writer.writerow(["R1", "UNK-1", "UNKNOWN VALID PRODUCT TITLE"])
    first = decode_stage(raw, "guarded", VariableModel("Braking", "Brake Pads"), tmp_path / "first")
    second = decode_stage(
        raw, "guarded", VariableModel("Electrical", "Spark Plugs"), tmp_path / "second"
    )
    assert first["batch_id"] == second["batch_id"]
    assert first["run_id"] != second["run_id"]
