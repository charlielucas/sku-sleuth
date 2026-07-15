from streamlit.testing.v1 import AppTest


def run_app():
    at = AppTest.from_file("app.py", default_timeout=120)
    return at.run()


def test_boots_and_passes_on_defaults():
    at = run_app()
    assert not at.exception
    banners = " ".join(str(b.value) for b in at.markdown) + " ".join(
        str(s.value) for s in at.success
    )
    assert "GATE: PASS" in banners


def test_naive_ruleset_flips_gate_to_fail():
    at = run_app()
    ruleset = next(r for r in at.radio if r.key == "ruleset")
    ruleset.set_value("naive")
    at = at.run()
    assert not at.exception
    errors = " ".join(str(e.value) for e in at.error)
    assert "GATE: FAIL" in errors


def test_threshold_slider_rejudges_without_redecode():
    at = run_app()
    slider = next(s for s in at.slider if s.key == "coverage")
    slider.set_value(1.0)  # coverage is honestly < 1.0 by construction, so this must fail
    at = at.run()
    errors = " ".join(str(e.value) for e in at.error)
    assert "GATE: FAIL" in errors  # same measurement, stricter policy
