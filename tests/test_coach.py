"""
Regression tests for the logic that used to be done by the model.

No network and no Garmin credentials required -- these cover the pure
functions: week windowing, plan resolution, weekly aggregation, adjustment
bounds, output parsing and message chunking.

Run:
    python -m pytest tests/ -q
    python tests/test_coach.py        # also works without pytest
"""
import copy
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# The Garmin client is never constructed in these tests; stub the import so the
# module can be loaded without the dependency installed.
if "garminconnect" not in sys.modules:
    import types
    _m = types.ModuleType("garminconnect")
    _m.Garmin = object
    sys.modules["garminconnect"] = _m

from src import inbox, llm, metrics, notify, plan as P  # noqa: E402

PLAN = P.load(str(ROOT / "data" / "plan.json"))


# --------------------------------------------------------------------------
# Week window: the Sunday bug
# --------------------------------------------------------------------------

def test_window_is_stable_whatever_day_you_run_it():
    """The whole point of the fix: a manual run mid-week reviews the same week."""
    expected = (date(2026, 7, 27), date(2026, 8, 2))
    for d in [date(2026, 8, 3), date(2026, 8, 4), date(2026, 8, 6), date(2026, 8, 9)]:
        assert metrics.last_complete_week(d) == expected


def test_window_is_a_monday_to_sunday_calendar_week():
    ws, we = metrics.last_complete_week(date(2026, 8, 3))
    assert ws.weekday() == 0 and we.weekday() == 6
    assert (we - ws).days == 6


def test_window_never_includes_today():
    """Sunday's activities do not exist until Monday; the window must end before now."""
    for d in [date(2026, 8, 2), date(2026, 8, 3), date(2026, 8, 5)]:
        _, we = metrics.last_complete_week(d)
        assert we < d


# --------------------------------------------------------------------------
# Plan resolution: the hallucinated target
# --------------------------------------------------------------------------

def test_deload_week_resolves_to_its_real_target():
    """The model reported 75 km for this week. It is A2 week 8, a 65 km deload."""
    ctx = P.resolve(PLAN, date(2026, 7, 27))
    assert ctx["phase_id"] == "A2"
    assert ctx["week_in_phase"] == 8
    assert ctx["target_km"] == 65
    assert ctx["target_vert_m"] == 1500


def test_phase_boundary_is_exact():
    assert P.resolve(PLAN, date(2026, 8, 2))["phase_id"] == "A2"
    assert P.resolve(PLAN, date(2026, 8, 3))["phase_id"] == "A3"


def test_long_run_target_comes_from_the_progression_table():
    lr = P.resolve(PLAN, date(2026, 8, 3))["long_run_target"]
    assert lr["duration_min"] == 165 and lr["vert_m"] == 900


def test_dates_outside_the_plan_return_unknown_not_garbage():
    for d in [date(2020, 1, 1), date(2035, 1, 1)]:
        ctx = P.resolve(PLAN, d)
        assert ctx["phase_id"] is None and ctx["target_km"] is None
        assert "unknown" in P.describe(ctx)


# --------------------------------------------------------------------------
# Plan mutation safety
# --------------------------------------------------------------------------

def test_out_of_bounds_adjustments_are_rejected():
    ctx = P.resolve(PLAN, date(2026, 8, 10))
    before = json.dumps(PLAN, sort_keys=True)
    for bad in [{"target_km": 999}, {"target_km": None}, {"target_km": "x"},
                {"target_km": 1}, {}]:
        doc = copy.deepcopy(PLAN)
        P.apply_adjustment(doc, ctx, bad)
        assert json.dumps(doc, sort_keys=True) == before, bad


def test_reasonable_adjustment_is_applied():
    ctx = P.resolve(PLAN, date(2026, 8, 10))
    doc = copy.deepcopy(PLAN)
    _, log = P.apply_adjustment(doc, ctx, {"target_km": 70})
    assert any("->" in line for line in log)
    assert P.resolve(doc, date(2026, 8, 10))["target_km"] == 70


# --------------------------------------------------------------------------
# Weekly aggregation
# --------------------------------------------------------------------------

def _activity(**kw):
    base = {"id": "a", "date": "2026-07-28", "type": "trail_running",
            "distance_km": 10.0, "duration_min": 60.0, "moving_min": 60.0,
            "elevation_gain_m": 400.0, "elevation_loss_m": 400.0,
            "z1_s": 1800, "z2_s": 1800, "z3_s": 0, "z4_s": 0, "z5_s": 0}
    base.update(kw)
    return base


def test_totals_and_plan_comparison():
    acts = [_activity(id="1", distance_km=10, elevation_gain_m=400),
            _activity(id="2", date="2026-07-30", distance_km=15, elevation_gain_m=600)]
    s = metrics.summarize(acts, {"target_km": 50, "target_vert_m": 1000})
    assert s["run_km"] == 25.0
    assert s["vert_gain_m"] == 1000
    assert s["km_vs_target_pct"] == 50
    assert s["vert_vs_target_pct"] == 100
    assert s["vert_density_m_per_km"] == 40.0


def test_walking_is_counted_separately_from_running():
    """Running km drives plan comparison; walking still shows in the breakdown."""
    acts = [_activity(id="1", distance_km=10),
            _activity(id="2", type="walking", distance_km=5, elevation_gain_m=50)]
    s = metrics.summarize(acts, {})
    assert s["run_km"] == 10.0
    assert s["all_km"] == 15.0
    assert "walking" in s["by_type"]


def test_intensity_distribution():
    acts = [_activity(z1_s=3600, z2_s=3600, z3_s=1800, z4_s=0, z5_s=0)]
    s = metrics.summarize(acts, {})
    assert s["easy_pct"] == 80 and s["hard_pct"] == 20


def test_acwr_is_suppressed_without_a_chronic_base():
    """12 days of history divided by 4 weeks produced a meaningless 2.63."""
    hist = {f"2026-07-{d:02d}": 100 for d in range(20, 32)}
    s = metrics.summarize([_activity()], {}, load_history=hist,
                          week_end=date(2026, 8, 2), coverage_days=11)
    assert s["acwr"]["unreliable"] is True


def test_acwr_reports_when_history_is_sufficient():
    hist = {(date(2026, 8, 2) - timedelta(days=i)).isoformat(): 100 for i in range(28)}
    s = metrics.summarize([_activity()], {}, load_history=hist,
                          week_end=date(2026, 8, 2), coverage_days=28)
    assert s["acwr"]["ratio"] == 1.0


def test_back_to_back_long_days():
    acts = [_activity(id="1", date="2026-07-28", moving_min=120),
            _activity(id="2", date="2026-07-29", moving_min=120),
            _activity(id="3", date="2026-08-01", moving_min=120)]
    s = metrics.summarize(acts, {})
    assert s["long_day_count"] == 3 and s["b2b_long_days"] == 1


def test_vam_ignores_flat_runs():
    flat = _activity(elevation_gain_m=50, distance_km=10, moving_min=60)
    climb = _activity(id="2", elevation_gain_m=600, distance_km=10, moving_min=60)
    assert metrics._vam(flat) is None
    assert metrics._vam(climb) == 600


def test_empty_and_null_input_do_not_crash():
    s = metrics.summarize([], {})
    assert s["run_km"] == 0 and s["km_vs_target_pct"] is None
    assert metrics.describe(s, gaps=[], notes=[], scores=[])
    s2 = metrics.summarize([{"id": "1", "date": "2026-07-27", "type": "trail_running"}], {})
    assert s2["run_km"] == 0


def test_missing_manual_log_is_stated_loudly():
    text = metrics.describe(metrics.summarize([], {}), notes=[])
    assert "NO ENTRIES" in text and "Do NOT assume" in text


def test_data_gaps_are_flagged_as_understating_totals():
    text = metrics.describe(metrics.summarize([], {}), gaps=["2026-07-31"])
    assert "UNDERSTATED" in text


# --------------------------------------------------------------------------
# Telegram ingestion
# --------------------------------------------------------------------------

def test_note_parsing():
    now = datetime(2026, 8, 3, 20, 0, 0)
    cases = [
        ("s: back squat 5x5", "2026-08-03", "strength"),
        ("strength gym A done", "2026-08-03", "strength"),
        ("feet: hotspot left heel", "2026-08-03", "feet"),
        ("sore 3", "2026-08-03", "soreness"),
        ("rpe 7", "2026-08-03", "rpe"),
        ("2026-08-01 s: deadlifts", "2026-08-01", "strength"),
        ("yesterday feet: clean", "2026-08-02", "feet"),
        ("legs felt heavy today", "2026-08-03", "note"),
    ]
    for text, want_date, want_kind in cases:
        d, kind, _, _ = inbox.parse(text, now)
        assert (d, kind) == (want_date, want_kind), text


def test_soreness_and_rpe_capture_the_number():
    now = datetime(2026, 8, 3)
    assert inbox.parse("sore 4", now)[2] == "4"
    assert inbox.parse("rpe 8", now)[2] == "8"


def test_malformed_input_never_raises():
    now = datetime(2026, 8, 3)
    for text in ["", "   ", "2026-13-45 s: x", "!!!", "s:"]:
        assert inbox.parse(text, now)[1] in ("note", "strength")


# --------------------------------------------------------------------------
# Output parsing and delivery
# --------------------------------------------------------------------------

def test_llm_parsers_degrade_gracefully():
    assert llm.human_part("just text") == "just text"
    assert llm.extract_json_block("no block") is None
    assert llm.extract_json_block("```json\n{bad,}\n```") is None
    assert llm.parse_header("---\nbody") == {}


def test_telegram_messages_are_split_under_the_limit():
    assert len(notify._chunks("x" * 4096)) == 1
    assert all(len(p) <= 4096 for p in notify._chunks("x" * 10000))
    assert all(len(p) <= 4096 for p in notify._chunks("para\n\n" * 2000))


if __name__ == "__main__":
    fails = []
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS  {name}")
            except AssertionError as e:
                print(f"  FAIL  {name}  {e}")
                fails.append(name)
    print(f"\n{'FAILED: ' + ', '.join(fails) if fails else 'All tests passed.'}")
    sys.exit(1 if fails else 0)
