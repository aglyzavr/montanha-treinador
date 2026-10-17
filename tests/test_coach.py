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
import functools
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

from src import inbox, llm, metrics, notify, plan as P, prompt  # noqa: E402

# plan.json is private and git-ignored, so a fresh clone (or a new machine before
# the data folder is restored) has none. The plan-dependent tests skip in that
# case instead of the whole suite failing at import -- which is what happened on
# the first Windows checkout. Everything that doesn't read the plan still runs.
_PLAN_PATH = ROOT / "data" / "plan.json"
PLAN = P.load(str(_PLAN_PATH)) if _PLAN_PATH.exists() else None


class _Skipped(Exception):
    pass


def needs_plan(fn):
    @functools.wraps(fn)
    def wrapper(*a, **k):
        if PLAN is None:
            try:
                import pytest
            except ImportError:
                raise _Skipped()
            pytest.skip("data/plan.json not present (private, git-ignored)")
        return fn(*a, **k)
    return wrapper


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

@needs_plan
def test_deload_week_resolves_to_its_real_target():
    """The model reported 75 km for this week. It is A2 week 8, a 65 km deload."""
    ctx = P.resolve(PLAN, date(2026, 7, 27))
    assert ctx["phase_id"] == "A2"
    assert ctx["week_in_phase"] == 8
    assert ctx["target_km"] == 65
    assert ctx["target_vert_m"] == 1500


@needs_plan
def test_phase_boundary_is_exact():
    assert P.resolve(PLAN, date(2026, 8, 2))["phase_id"] == "A2"
    assert P.resolve(PLAN, date(2026, 8, 3))["phase_id"] == "A3"


@needs_plan
def test_long_run_target_comes_from_the_progression_table():
    lr = P.resolve(PLAN, date(2026, 8, 3))["long_run_target"]
    assert lr["duration_min"] == 165 and lr["vert_m"] == 900


@needs_plan
def test_dates_outside_the_plan_return_unknown_not_garbage():
    for d in [date(2020, 1, 1), date(2035, 1, 1)]:
        ctx = P.resolve(PLAN, d)
        assert ctx["phase_id"] is None and ctx["target_km"] is None
        assert "unknown" in P.describe(ctx)


# --------------------------------------------------------------------------
# Plan mutation safety
# --------------------------------------------------------------------------

@needs_plan
def test_out_of_bounds_adjustments_are_rejected():
    ctx = P.resolve(PLAN, date(2026, 8, 10))
    before = json.dumps(PLAN, sort_keys=True)
    for bad in [{"target_km": 999}, {"target_km": None}, {"target_km": "x"},
                {"target_km": 1}, {}]:
        doc = copy.deepcopy(PLAN)
        P.apply_adjustment(doc, ctx, bad)
        assert json.dumps(doc, sort_keys=True) == before, bad


@needs_plan
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


# --------------------------------------------------------------------------
# The missing session: "execute the long run as planned" with no numbers
#
# plan.json held 180 min / 1100 m / "add 20 min technical descent" the whole
# time. Nothing in the prompt required the model to write any of it down, so
# the athlete got a message that pointed back at the plan instead of
# prescribing a session. These lock the numbers into the prompt.
# --------------------------------------------------------------------------

def _runs(n=12, speed=8.0, density=30.0, hours=1.5, spread=True):
    """Synthetic history. Varied by default: a real athlete's runs are not all
    identical, and the estimator matches on vertical density."""
    out = []
    for i in range(n):
        f = (1 + 0.6 * (i % 5) if spread else 1.0)      # densities 1x .. 3.4x
        km = speed * hours
        out.append({"type": "trail_running", "duration_min": hours * 60,
                    "distance_km": km, "elevation_gain_m": km * density * f})
    return out


@needs_plan
def test_long_run_day_carries_duration_vert_and_focus():
    ctx = P.resolve(PLAN, date(2026, 8, 15))       # A3 week 2, Saturday
    block = prompt.build_prescription(ctx, _runs())
    assert "180 min" in block
    assert "1100 m" in block
    assert "Add 20 min technical descent" in block


@needs_plan
def test_prescription_gives_bpm_not_zone_labels():
    """Zone labels are not actionable in a Telegram message; bpm is.

    The figures are read out of the plan rather than hardcoded -- AeT and LT
    get retested, and a test that asserts last season's numbers would start
    failing for the one reason that is not a bug.
    """
    ctx = P.resolve(PLAN, date(2026, 8, 15))       # A3 week 2, Saturday
    z = ctx["hr_zones"]
    block = prompt.build_prescription(ctx, _runs())
    assert f"below {z['z2_upper_aerobic_bpm'][0]} bpm" in block
    assert f"hard ceiling {z['z2_upper_aerobic_bpm'][1]} bpm" in block
    assert f"Z3 starts at {z['z3_sub_threshold_bpm'][0]} bpm" in block


@needs_plan
def test_threshold_day_gets_threshold_bpm_not_easy_bpm():
    ctx = P.resolve(PLAN, date(2026, 8, 20))       # Thursday: Z3->Z4 intervals
    z = ctx["hr_zones"]
    block = prompt.build_prescription(ctx, _runs())
    assert f"work reps {z['z3_sub_threshold_bpm'][0]}-{z['z3_sub_threshold_bpm'][1]} bpm" in block
    assert f"top end {z['z4_near_lt_bpm'][0]}-{z['z4_near_lt_bpm'][1]} bpm" in block
    assert f"recoveries below {z['z1_easy_aerobic_bpm'][1]} bpm" in block
    assert "hard ceiling" not in block             # i.e. not the easy-day line


@needs_plan
def test_distance_stated_by_the_plan_is_used_verbatim():
    ctx = P.resolve(PLAN, date(2026, 8, 18))       # Tuesday club run "12-14km"
    block = prompt.build_prescription(ctx, _runs())
    assert "PRESCRIBED DISTANCE: 12-14 km" in block
    assert "ESTIMATE" not in block


@needs_plan
def test_estimated_distance_is_labelled_an_estimate():
    ctx = P.resolve(PLAN, date(2026, 8, 15))
    block = prompt.build_prescription(ctx, _runs())
    assert "ESTIMATE ONLY" in block
    assert "never as a target" in block


def test_estimate_is_withheld_when_history_is_thin():
    assert prompt.estimate_distance_km(180, 1100, []) is None
    assert prompt.estimate_distance_km(180, 1100, _runs(n=4)) is None
    assert prompt.estimate_distance_km(180, 1100, _runs(n=20, hours=0.5)) is None  # all too short
    assert prompt.estimate_distance_km(180, 1100, _runs(n=20, hours=1.5,
                                                        density=5.0)) is None  # all flat


def test_estimate_scales_with_time():
    hist = _runs(n=20)
    long_ = prompt.estimate_distance_km(180, 900, hist)
    short = prompt.estimate_distance_km(90, 450, hist)
    assert long_ and short
    assert short[1] < long_[1]         # less time, fewer km
    assert long_[0] < long_[1]


def test_estimate_is_withheld_at_a_steepness_never_trained():
    """Flat-run pace must not be quietly reused for a climb they have never done."""
    flat_history = _runs(n=20, density=20.0, spread=False)
    assert prompt.estimate_distance_km(180, 3000, flat_history) is None


def test_estimate_fades_rather_than_extrapolating_pace():
    """A 4h run must not be predicted at the pace of the athlete's 1.5h runs."""
    hist = _runs(n=20, speed=8.0, hours=1.5, density=30.0)
    est = prompt.estimate_distance_km(240, 720, hist)
    assert est and est[1] < 8.0 * 4    # below naive speed x time


@needs_plan
def test_rest_days_get_no_prescription_block():
    assert prompt.build_prescription(P.resolve(PLAN, date(2026, 8, 17)), _runs()) == ""
    assert prompt.build_prescription(P.resolve(PLAN, date(2035, 1, 1)), _runs()) == ""


def test_contract_forbids_answering_with_as_planned():
    assert "as planned" in prompt.OUTPUT_CONTRACT
    assert "FORBIDDEN" in prompt.OUTPUT_CONTRACT
    assert "Today's session:" in prompt.OUTPUT_CONTRACT


@needs_plan
def test_daily_prompt_actually_contains_the_numbers():
    system, user = prompt.build_daily(
        "principles", PLAN, {}, [], [], today=date(2026, 8, 15),
        activity_history=_runs())
    assert "PRESCRIBED DURATION: 180 min" in user
    assert "1100 m D+" in user
    assert "Never write \"as planned\"" in system


@needs_plan
def test_build_daily_works_without_history():
    """The estimate is optional; its absence must not break the daily job."""
    _, user = prompt.build_daily("p", PLAN, {}, [], [], today=date(2026, 8, 15))
    assert "PRESCRIBED DURATION: 180 min" in user
    assert "not enough" in user

# --------------------------------------------------------------------------
# Windows move (2026-10): context window, failure alerts, backup reminder
# --------------------------------------------------------------------------

def test_prompt_too_big_for_the_window_is_refused_not_truncated():
    """Ollama silently drops the start of an oversized prompt -- the rules."""
    big = "x" * 100_000                      # ~33k tokens at 3 chars/token
    try:
        llm.check_context(32768, "system", big)
        assert False, "should have raised"
    except llm.ContextOverflow:
        pass
    assert llm.check_context(32768, "system", "x" * 30_000) > 0


def test_ollama_reporting_a_full_window_is_treated_as_truncation():
    for data in ({"prompt_eval_count": 32768}, {"prompt_eval_count": 900, "done_reason": "length"}):
        try:
            llm.check_response(32768, data)
            assert False, "should have raised"
        except llm.ContextOverflow:
            pass
    assert llm.check_response(32768, {"prompt_eval_count": 9000, "done_reason": "stop"}) == 9000


def test_failure_alert_never_leaks_the_bot_token():
    from src import runtime
    err = Exception("400 Client Error for url: https://api.telegram.org/bot123456:AAbbCC-dd_ee/sendMessage")
    msg = runtime.failure_message("daily", err)
    assert "AAbbCC" not in msg and "bot***" in msg
    assert "no recommendation" in msg


def test_failure_alert_tells_you_what_to_do():
    from src import runtime
    assert "Ollama" in runtime.hint_for(ConnectionError("localhost port=11434 refused"))
    assert "num_ctx" in runtime.hint_for(llm.ContextOverflow("too big"))
    assert "setup_garmin_login" in runtime.hint_for(Exception("401 Unauthorized"))


def test_backup_reminder_only_after_the_limit():
    from src import backup_status
    cfg = {"backup": {"remind_after_days": 14}}
    now = datetime(2026, 10, 30)
    assert backup_status.reminder(cfg, now=now, last=datetime(2026, 10, 20)) == ""
    assert "20 days" in backup_status.reminder(cfg, now=now, last=datetime(2026, 10, 10))
    assert "never" in backup_status.reminder(cfg, now=now, last=None)

# --------------------------------------------------------------------------
# Morning check-in app (2026-10): taps replace the Telegram reply codes
# --------------------------------------------------------------------------

def test_checkin_round_trip_and_resave_does_not_duplicate(tmp_path=None):
    import tempfile
    from src import store as S, app
    d = Path(tmp_path or tempfile.mkdtemp())
    db = S.Store(str(d / "c.db")); db.init()
    today = date(2026, 10, 15)
    form = {"strength": "yes", "strength_text": "stage 1 circuit", "soreness": "3",
            "feet": "hotspot", "feet_text": "left heel", "rpe": "6", "note": "slight cold"}
    app.save_checkin(db, today, form)
    app.save_checkin(db, today, form)                    # saved twice: no duplicates
    assert app.read_checkin(db, today) == {**form, "soreness": 3, "rpe": 6}
    notes = db.notes_between("2026-10-14", "2026-10-15")
    assert len(notes) == 5
    tally = inbox.summarize_notes(notes)
    assert tally["strength"] == 1 and tally["soreness"] == [("2026-10-15", "3")]


def test_checkin_no_strength_is_not_counted_as_a_session(tmp_path=None):
    import tempfile
    from src import store as S, app
    db = S.Store(str(Path(tmp_path or tempfile.mkdtemp()) / "c.db")); db.init()
    app.save_checkin(db, date(2026, 10, 15), {"strength": "no"})
    assert inbox.summarize_notes(db.notes_between("2026-10-14", "2026-10-15"))["strength"] == 0
    # clearing a field removes the entry: skipped means unlogged, never assumed
    app.save_checkin(db, date(2026, 10, 15), {})
    assert db.notes_between("2026-10-14", "2026-10-15") == []


def test_checkin_never_touches_telegram_history(tmp_path=None):
    import tempfile
    from src import store as S, app
    db = S.Store(str(Path(tmp_path or tempfile.mkdtemp()) / "c.db")); db.init()
    db.add_note("2026-10-14", "strength", "done", "squats", update_id=42)
    app.save_checkin(db, date(2026, 10, 15), {})
    assert [n["update_id"] for n in db.notes_between("2026-10-14", "2026-10-15")] == [42]


def test_sub_phase_day_pattern_overrides_block_default():
    # Lizandro race week puts the race on Friday; the block default has an easy
    # run there. Without the override the coach would prescribe a Z1 jog on
    # race day.
    doc = {"plans": [{
        "id": "blk", "window": {"start_date": "2026-11-02", "end_date": "2026-11-15"},
        "weekly_day_pattern_default": {"Fri": {"type": "easy_run_or_rest", "description": "Z1"}},
        "sub_phases": [
            {"id": "RW", "dates": {"start": "2026-11-02", "end": "2026-11-08"},
             "weekly_day_pattern": {"Fri": {"type": "race", "description": "102.5 km"}}},
            {"id": "PR", "dates": {"start": "2026-11-09", "end": "2026-11-15"}},
        ]}]}
    assert P.resolve(doc, date(2026, 11, 6))["planned_today"]["type"] == "race"
    # sub-phase without its own pattern falls back to the block default
    assert P.resolve(doc, date(2026, 11, 13))["planned_today"]["type"] == "easy_run_or_rest"


def test_sub_phase_notes_reach_the_prompt():
    # notes were resolved into ctx but never printed, so the model never saw
    # them (e.g. the Lizandro go/no-go check).
    doc = {"plans": [{
        "id": "blk", "window": {"start_date": "2026-10-26", "end_date": "2026-11-01"},
        "sub_phases": [{"id": "L2", "dates": {"start": "2026-10-26", "end": "2026-11-01"},
                        "notes": "Go/no-go check on Thursday"}]}]}
    assert "Go/no-go check on Thursday" in P.describe(P.resolve(doc, date(2026, 10, 29)))


def test_sleep_not_synced_is_detected():
    from src import daily
    assert not daily.sleep_synced({"sleep_duration_min": None})
    assert daily.sleep_synced({"sleep_duration_min": 455})

# --------------------------------------------------------------------------
# Strength (2026-10): the Monday slot was dropped every week, and a missed
# session was never offered again
# --------------------------------------------------------------------------

def _strength_doc():
    """Mon strength B inside a rest day, Wed strength A, race on 2026-11-27."""
    return {"plans": [{
        "id": "blk", "window": {"start_date": "2026-10-05", "end_date": "2026-11-29"},
        "goal": {"event": "Race", "race_date": "2026-11-27"},
        "weekly_day_pattern_default": {
            "Mon": {"type": "rest_or_walk", "description": "Rest or walk + strength B (bodyweight/ME circuit)"},
            "Tue": {"type": "club_run", "description": "12-14km"},
            "Wed": {"type": "easy_run_plus_strength_A", "description": "45 min Z1 + strength A"},
            "Thu": {"type": "easy_run", "description": "45 min Z1"},
            "Fri": {"type": "easy_run_or_rest", "description": "Short Z1"},
            "Sat": {"type": "long_run", "description": "Long"},
            "Sun": {"type": "easy_recovery", "description": "Z1"}},
        "strength_program": {"frequency_per_week": 2,
                             "phases": [{"phase": "S1_base", "focus": "Stage circuits"}]},
        "sub_phases": [{"id": "S1", "dates": {"start": "2026-10-05", "end": "2026-11-29"}}],
    }]}


MON, TUE, WED, THU, FRI = (date(2026, 10, 19) + timedelta(days=i) for i in range(5))


def test_strength_slot_is_read_from_a_rest_day():
    s = P.resolve(_strength_doc(), MON)["strength_today"]
    assert s["slot"] == "B" and not s["light"]          # "bodyweight" is not "light"
    assert P.resolve(_strength_doc(), WED)["strength_today"]["slot"] == "A"
    assert P.resolve(_strength_doc(), TUE)["strength_today"] is None
    assert P.strength_in({"type": "rest", "description": "light strength B"})["light"]
    assert P.strength_in({"type": "race", "description": "no strength"}) is None


def test_monday_rest_day_gets_a_required_strength_block():
    system, user = prompt.build_daily("p", _strength_doc(), {}, [], [], today=MON,
                                      strength_reference="REFERENCE-TEXT",
                                      strength_status=prompt.strength_status(
                                          _strength_doc(), MON, [], []))
    assert "TODAY'S STRENGTH SESSION -- REQUIRED (strength B)" in user
    assert "REFERENCE-TEXT" in user
    # the required block comes before the recovery data, the reference after it
    assert user.index("REQUIRED") < user.index("LAST NIGHT") < user.index("REFERENCE-TEXT")
    assert "STRENGTH:" in system and "does not\ncancel a strength session" in system


def test_missed_monday_is_offered_again_on_tuesday():
    st = prompt.strength_status(_strength_doc(), TUE, [], [])
    assert st["owed"] == 1 and st["missed"][0][0] == MON.isoformat()
    due = prompt.strength_due(_strength_doc(), TUE, st)
    assert due["kind"] == "makeup" and due["tomorrow_is_strength"]
    _, user = prompt.build_daily("p", _strength_doc(), {}, [], [], today=TUE,
                                 strength_reference="REF", strength_status=st)
    assert "MAKE-UP STRENGTH SESSION OWED (strength B planned 2026-10-19" in user


def test_logged_strength_is_not_owed():
    doc = _strength_doc()
    note = [{"date": MON.isoformat(), "kind": "strength", "text": "stage 1"}]
    garmin_act = [{"date": MON.isoformat(), "type": "strength_training"}]
    assert prompt.strength_status(doc, TUE, note, [])["owed"] == 0
    assert prompt.strength_status(doc, TUE, [], garmin_act)["owed"] == 0
    # an explicit "no" in the check-in is a missed session, not a done one
    no = [{"date": MON.isoformat(), "kind": "no_strength", "text": "no strength session"}]
    assert prompt.strength_status(doc, TUE, no, [])["owed"] == 1


def test_no_makeup_the_day_before_a_long_run_or_before_a_race():
    doc = _strength_doc()
    st = prompt.strength_status(doc, FRI, [], [])
    assert st["owed"] == 2
    assert prompt.strength_due(doc, FRI, st) is None                 # Sat is the long run
    assert prompt.strength_due(doc, THU, prompt.strength_status(doc, THU, [], []))["kind"] == "makeup"
    race_tue = date(2026, 11, 17)                                     # 10 days out
    assert prompt.strength_due(doc, race_tue,
                               prompt.strength_status(doc, race_tue, [], [])) is None


def test_strength_focus_follows_a_see_reference():
    doc = {"plans": [
        {"id": "old", "window": {"start_date": "2026-01-05", "end_date": "2026-02-01"},
         "strength_program": {"frequency_per_week": 2,
                              "phases": [{"phase": "X1_base", "focus": "base"}]}},
        {"id": "new", "window": {"start_date": "2026-02-02", "end_date": "2026-03-01"},
         "strength_program": "See old.strength_program; maintenance only in this block.",
         "sub_phases": [{"id": "N1", "dates": {"start": "2026-02-02", "end": "2026-03-01"}}]}]}
    sf = P.resolve(doc, date(2026, 2, 10))["strength_focus"]
    assert sf["frequency_per_week"] == 2 and "maintenance only" in sf["note"]
    assert "maintenance only" in P.describe(P.resolve(doc, date(2026, 2, 10)))


def test_block_without_a_day_pattern_borrows_the_previous_one():
    doc = _strength_doc()
    doc["plans"].append({"id": "next", "window": {"start_date": "2026-11-30",
                                                  "end_date": "2026-12-27"}})
    ctx = P.resolve(doc, date(2026, 11, 30))                          # a Monday
    assert ctx["day_pattern_inherited_from"] == "blk"
    assert ctx["strength_today"]["slot"] == "B"


def test_doctor_flags_weeks_without_strength_but_not_recovery_weeks():
    from src import doctor
    doc = _strength_doc()
    assert doctor.plan_coverage_problems(doc, MON, weeks=2) == []
    for day in ("Mon", "Wed"):
        doc["plans"][0]["weekly_day_pattern_default"][day] = {"type": "rest", "description": "Rest"}
    assert any("no strength" in p for p in doctor.plan_coverage_problems(doc, MON, weeks=1))
    # a sub-phase that spells out its days (recovery, race week) is a decision
    doc["plans"][0]["sub_phases"][0]["weekly_day_pattern"] = {"Mon": {"type": "rest"}}
    assert doctor.plan_coverage_problems(doc, MON, weeks=1) == []


def test_strength_answer_check():
    listed = ("VERDICT: rest\nSTRENGTH: Stage 1 circuit\n---\nRest day.\n\n"
              "Today’s strength session:\n1. Plank -- 6 x 15s, rest 1 min.")
    assert prompt.strength_answered(listed)
    assert prompt.strength_answered("VERDICT: rest\nSTRENGTH: skipped: sleep 4h\n---\nRest.")
    assert not prompt.strength_answered("VERDICT: rest\nSTRENGTH: skipped\n---\nRest.")
    assert not prompt.strength_answered("VERDICT: rest\nSESSION: Rest or walk\n---\nRest.")
    assert not prompt.strength_answered("VERDICT: rest\n---\nToday's strength session: Stage 1")


REFERENCE = """# Ref

## Core (ch. 7) — finisher
- Stage 1: plank from knees, 6x15s

## Stage 1-3 general strength programs (legs/hips)
- Stage 1: band squats 4x20

## Full general-strength workout template
1. Warm-up
"""


def _with_fake_chat(answers, fn):
    from src import daily
    calls, real = [], daily.llm.chat

    def fake(cfg, system, user, followup=None):
        calls.append(followup)
        return answers[len(calls) - 1]
    daily.llm.chat = fake
    try:
        return fn(), calls
    finally:
        daily.llm.chat = real


def test_missing_strength_is_retried_once_then_appended():
    from src import daily
    due = {"kind": "today", "slot": "B", "wording": "Rest + strength B", "light": False}
    bad = "VERDICT: rest\nSESSION: rest\n---\nEnjoy the rest day."
    good = ("VERDICT: rest\nSTRENGTH: Stage 1\n---\nRest.\n\nToday's strength session:\n"
            "1. Band squats -- 4 x 20, rest 1 min.")
    run = lambda: daily._ensure_strength({}, "s", "u", bad, due, REFERENCE, None, lambda s: None)

    text, calls = _with_fake_chat([good], run)
    assert text == good and calls == [(bad, prompt.STRENGTH_RETRY)]

    text, calls = _with_fake_chat([bad], run)
    assert len(calls) == 1                                  # one retry, never a loop
    assert "added by the app" in text and "strength B today" in text
    assert "band squats 4x20" in text and "plank from knees" in text
    assert llm.human_part(text).startswith("Enjoy the rest day.")


def test_strength_call_is_kept_in_session_detail():
    from src import daily
    assert daily._session_detail({"SESSION": "rest", "STRENGTH": "Stage 1"}) == \
        "rest | strength: Stage 1"
    assert daily._session_detail({"SESSION": "45 min Z1", "STRENGTH": "none planned"}) == "45 min Z1"


def test_weekly_review_states_strength_adherence():
    s = metrics.summarize([], {}, week_start=MON, week_end=MON + timedelta(days=6))
    s.update(strength_planned=2, strength_done=0, strength_missed=["2026-10-19", "2026-10-21"])
    text = metrics.describe(s)
    assert "0 of 2 planned strength sessions" in text
    assert "2026-10-19, 2026-10-21" in text


@needs_plan
def test_real_plan_monday_carries_strength():
    _, user = prompt.build_daily("p", PLAN, {}, [], [], today=date(2026, 8, 24),
                                 strength_reference="REF")
    assert "TODAY'S STRENGTH SESSION -- REQUIRED (strength B)" in user


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
            except BaseException as e:   # _Skipped, or pytest's Skipped
                if type(e).__name__ not in ("_Skipped", "Skipped"):
                    raise
                print(f"  SKIP  {name}  (no data/plan.json)")
    print(f"\n{'FAILED: ' + ', '.join(fails) if fails else 'All tests passed.'}")
    sys.exit(1 if fails else 0)
