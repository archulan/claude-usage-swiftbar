"""Contract tests for the pure rendering layer of claude-usage.5m.py.

The plugin filename is not an importable module name, so the module is loaded
from its file path. All time-dependent tests pin an aware "now" with a fixed
UTC+10 offset so results never depend on the machine's timezone.
"""

import importlib.util
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

PLUGIN_PATH = Path(__file__).resolve().parent.parent / "claude-usage.5m.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures"

_spec = importlib.util.spec_from_file_location("claude_usage", PLUGIN_PATH)
plugin = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(plugin)

NOW = datetime(2026, 8, 4, 15, 0, tzinfo=timezone(timedelta(hours=10)))
ULTRA_NOW = datetime(2026, 11, 2, 9, 0, tzinfo=timezone(timedelta(hours=11)))


def fixture(name):
    """Loads a synthetic payload dict from tests/fixtures."""
    with open(FIXTURES / (name + ".json")) as f:
        return json.load(f)


def title_of(lines):
    return lines[0]


def title_text(lines):
    return lines[0].split(" | ")[0]


# --- menu bar title ---


def test_title_shows_every_slot_when_the_plan_has_a_session():
    lines = plugin.render(fixture("normal"), NOW, "fresh", None, NOW)

    assert title_text(lines) == "S7·W42·F13%"
    assert "color=" not in title_of(lines)


def test_title_omits_slots_the_api_does_not_report():
    lines = plugin.render(fixture("sparse"), NOW, "fresh", None, NOW)

    assert title_text(lines) == "S7%"


def test_ultra_title_has_no_session_slot():
    lines = plugin.render(fixture("ultra"), ULTRA_NOW, "fresh", None, ULTRA_NOW)

    assert title_text(lines) == "W1·F2%"
    assert "sfimage=" not in title_of(lines)


def test_ultra_dropdown_has_no_session_row():
    lines = plugin.render(fixture("ultra"), ULTRA_NOW, "fresh", None, ULTRA_NOW)

    assert not any(rendered.startswith("Session") for rendered in lines)


def test_ultra_weekly_row_shows_the_reset_time():
    lines = plugin.render(fixture("ultra"), ULTRA_NOW, "fresh", None, ULTRA_NOW)

    assert any(
        rendered.startswith("Weekly (all models) 1% · resets Sun 19:00 (in 6d 10h)") for rendered in lines
    )


def test_title_initial_follows_the_model_the_api_reports():
    scoped = {"kind": "weekly_scoped", "scope": {"model": {"display_name": "Opus"}}}

    assert plugin.slot_initial("weekly_scoped", scoped) == "O"
    assert plugin.slot_initial("session", {}) == "S"
    assert plugin.slot_initial("weekly_all", {}) == "W"


@pytest.mark.parametrize(
    "entry",
    [
        None,
        {"kind": "weekly_scoped"},
        {"kind": "weekly_scoped", "scope": None},
        {"kind": "weekly_scoped", "scope": {"model": {"display_name": "   "}}},
        {"kind": "weekly_scoped", "scope": {"model": {"display_name": "!bang"}}},
    ],
)
def test_unnamed_model_falls_back_without_colliding_with_weekly(entry):
    # Deriving the letter from the generic label would yield "W" twice.
    initial = plugin.slot_initial("weekly_scoped", entry)

    assert initial == "M"
    assert initial != plugin.slot_initial("weekly_all", {})


@pytest.mark.parametrize("name", ["normal", "warning", "exceeded", "unknown", "sparse", "ultra"])
def test_title_is_never_coloured(name):
    # The menu bar is translucent, so text colour is resolved from a signal
    # that cannot see the wallpaper behind it. Only symbols stay legible.
    lines = plugin.render(fixture(name), NOW, "fresh", None, NOW)

    assert "color=" not in title_of(lines)


@pytest.mark.parametrize("state", ["fresh", "stale-token", "stale-fetch", "error"])
def test_title_is_never_coloured_in_any_state(state):
    payload = None if state == "error" else fixture("exceeded")
    lines = plugin.render(payload, NOW, state, "reason", NOW)

    assert "color=" not in title_of(lines)


@pytest.mark.parametrize("name", ["warning", "exceeded"])
def test_title_flags_severity_with_a_symbol(name):
    lines = plugin.render(fixture(name), NOW, "fresh", None, NOW)

    assert "sfimage=exclamationmark.triangle" in title_of(lines)


def test_title_carries_no_symbol_when_everything_is_normal():
    lines = plugin.render(fixture("normal"), NOW, "fresh", None, NOW)

    assert "sfimage=" not in title_of(lines)


def test_title_treats_unknown_severity_as_warning():
    lines = plugin.render(fixture("unknown"), NOW, "fresh", None, NOW)

    assert "sfimage=exclamationmark.triangle" in title_of(lines)


def test_title_shows_clock_when_token_stale():
    lines = plugin.render(
        fixture("normal"), NOW, "stale-token", "token expired, open Claude Code to refresh", NOW - timedelta(hours=3)
    )

    assert "sfimage=clock" in title_of(lines)


def test_title_shows_warning_symbol_when_fetch_fails():
    lines = plugin.render(
        fixture("normal"), NOW, "stale-fetch", "usage request failed (HTTP 500)", NOW - timedelta(minutes=5)
    )

    assert "sfimage=exclamationmark.triangle" in title_of(lines)


# --- degraded and error rendering ---


def test_error_without_cache_renders_glyph_reason_and_actions():
    lines = plugin.render(None, NOW, "error", "Claude Code credentials not found", None)

    assert "sfimage=exclamationmark.triangle" in title_of(lines)
    body = "\n".join(lines[1:])
    assert "Claude Code credentials not found" in body
    assert "href=" + plugin.USAGE_PAGE_URL in body
    assert "refresh=true" in body


def test_stale_render_marks_reason_and_last_updated():
    fetched = NOW - timedelta(hours=3)
    lines = plugin.render(
        fixture("normal"), NOW, "stale-token", "token expired, open Claude Code to refresh", fetched
    )

    body = "\n".join(lines[1:])
    assert "token expired, open Claude Code to refresh" in body
    assert "(stale)" in body


# --- pipe safety ---


PARAMS_PATTERN = re.compile(r'^(?:[a-z][a-z0-9]*=(?:"[^"]*"|\S+)\s*)+$')


def assert_first_pipe_is_the_separator(rendered):
    """Asserts SwiftBar would parse this line the way the plugin intends.

    Splitting on " | " would hide a leaked pipe that happens to be surrounded
    by spaces, so this splits on the bare character SwiftBar actually keys on
    and then proves the remainder really is a parameter list.
    """
    if rendered == "---":
        return

    head, sep, tail = rendered.partition("|")
    if not sep:
        # A line with no parameters is safe as long as it carries no pipe.
        return
    assert PARAMS_PATTERN.match(tail.strip()), "text leaked past the first pipe: " + rendered


def test_no_line_leaks_a_pipe_into_its_title_segment():
    # The exceeded fixture carries a hostile model name "Fa|ble"; SwiftBar
    # splits at the first pipe, so a leaked pipe silently truncates the line.
    for state, payload in [
        ("fresh", fixture("exceeded")),
        ("stale-fetch", fixture("exceeded")),
        ("error", None),
    ]:
        lines = plugin.render(payload, NOW, state, "reason with | pipe", NOW)
        for rendered in lines:
            assert_first_pipe_is_the_separator(rendered)


def test_hostile_model_name_is_neutralised_not_truncated():
    lines = plugin.render(fixture("exceeded"), NOW, "fresh", None, NOW)
    body = "\n".join(lines)

    assert "Fa/ble" in body
    assert "Fa|ble" not in body


def test_shortcode_expansion_is_disabled_on_api_controlled_lines():
    # Only lines carrying API text need it; the title must keep its sfimage
    # behaviour untouched.
    lines = plugin.render(fixture("normal"), NOW, "fresh", None, NOW)
    api_lines = [r for r in lines if r.startswith(("Session", "Weekly", "Fable", "Credits"))]

    assert len(api_lines) == 4
    for rendered in api_lines:
        assert "emojize=false" in rendered
        assert "symbolize=false" in rendered


# --- reset parsing and countdown formatting ---


@pytest.mark.parametrize(
    "value",
    ["2026-08-09T07:59:59Z", "2026-08-09T07:59:59+00:00", "2026-08-09T07:59:59"],
)
def test_parse_reset_returns_aware_datetimes(value):
    parsed = plugin.parse_reset(value)

    assert parsed is not None
    assert parsed.tzinfo is not None


@pytest.mark.parametrize("value", ["garbage", None, 12345, ""])
def test_parse_reset_rejects_non_timestamps(value):
    assert plugin.parse_reset(value) is None


@pytest.mark.parametrize(
    "delta,expected",
    [
        (timedelta(seconds=34), "in <1m"),
        (timedelta(minutes=34), "in 34m"),
        (timedelta(hours=2, minutes=10), "in 2h 10m"),
        (timedelta(hours=3), "in 3h"),
        (timedelta(days=5, hours=4), "in 5d 4h"),
        (timedelta(days=12), "in 12d"),
    ],
)
def test_countdown_buckets(delta, expected):
    assert plugin.format_countdown(NOW + delta, NOW) == expected


def test_reset_in_past_renders_soon_without_negative_numbers():
    entry = {"kind": "session", "percent": 7, "resets_at": (NOW - timedelta(seconds=90)).isoformat()}
    rendered = plugin.limit_line(entry, NOW, False)

    assert "resets soon" in rendered
    assert "-" not in rendered.split(" | ")[0].replace("resets soon", "")


def test_reset_clause_dropped_when_resets_at_missing_or_garbage():
    for entry in [
        {"kind": "session", "percent": 7},
        {"kind": "session", "percent": 7, "resets_at": "garbage"},
    ]:
        rendered = plugin.limit_line(entry, NOW, False)
        assert rendered.split(" | ")[0] == "Session 7%"


def test_same_day_reset_shows_bare_local_time():
    # 07:10 UTC is 17:10 in the pinned UTC+10 zone, same local day as NOW.
    entry = {"kind": "session", "percent": 7, "resets_at": "2026-08-04T07:10:00+00:00"}
    rendered = plugin.limit_line(entry, NOW, False)

    assert "resets 17:10 (in 2h 10m)" in rendered


def test_far_reset_shows_weekday_time():
    # Sunday 08:00 UTC is Sunday 18:00 local.
    entry = {"kind": "weekly_all", "percent": 42, "resets_at": "2026-08-09T08:00:00+00:00"}
    rendered = plugin.limit_line(entry, NOW, False)

    assert "resets Sun 18:00" in rendered


# --- money and spend ---


@pytest.mark.parametrize(
    "amount,exponent,currency,expected",
    [
        (12345, 2, "AUD", "A$123.45"),
        (500, 2, "USD", "$5.00"),
        (12345, 2, "CHF", "123.45 CHF"),
        (5, 0, "JPY", "5 JPY"),
    ],
)
def test_money_formatting(amount, exponent, currency, expected):
    assert plugin.format_money(amount, exponent, currency) == expected


def test_spend_line_renders_used_and_limit():
    rendered = plugin.spend_line(fixture("normal")["spend"])

    assert rendered == "Credits: $0.00 / $50.00"


@pytest.mark.parametrize(
    "spend",
    [
        None,
        {"enabled": False, "used": {"amount_minor": 0, "currency": "USD", "exponent": 2}},
        {"enabled": True, "used": {"amount_minor": "zero"}, "limit": {}},
    ],
)
def test_spend_line_omitted_when_missing_disabled_or_malformed(spend):
    assert plugin.spend_line(spend) is None


def test_render_omits_credits_line_when_spend_disabled():
    lines = plugin.render(fixture("unknown"), NOW, "fresh", None, NOW)

    assert not any("Credits:" in rendered for rendered in lines)


# --- unknown limit kinds ---


def test_unknown_kind_renders_generically_in_dropdown():
    lines = plugin.render(fixture("unknown"), NOW, "fresh", None, NOW)

    assert any(rendered.split(" | ")[0].startswith("Monthly Beta 55%") for rendered in lines)


# --- cache round-trip (only non-pure test; exercises real files in tmp_path) ---


def test_cache_round_trip_preserves_payload(tmp_path):
    path = str(tmp_path / "usage.json")
    plugin.write_cache(path, b'{"limits": []}')
    payload, fetched_at = plugin.read_cache(path)

    assert payload == {"limits": []}
    assert fetched_at is not None and fetched_at.tzinfo is not None


@pytest.mark.parametrize(
    "raw",
    [
        b"not json",
        b'"a string"',
        # A 200 carrying an error envelope is still a dict.
        b'{"type": "error", "error": {"message": "nope"}}',
    ],
)
def test_write_cache_refuses_bodies_that_are_not_usage_payloads(tmp_path, raw):
    path = str(tmp_path / "usage.json")
    plugin.write_cache(path, raw)

    assert plugin.read_cache(path) == (None, None)


def test_write_cache_preserves_previous_snapshot_on_bad_body(tmp_path):
    path = str(tmp_path / "usage.json")
    plugin.write_cache(path, b'{"limits": [{"kind": "session", "percent": 3}]}')
    plugin.write_cache(path, b'{"type": "error"}')
    payload, _ = plugin.read_cache(path)

    assert payload["limits"][0]["percent"] == 3


def test_write_cache_leaves_no_temp_files_behind(tmp_path):
    path = str(tmp_path / "usage.json")
    plugin.write_cache(path, b'{"limits": []}')

    assert sorted(p.name for p in tmp_path.iterdir()) == ["usage.json"]


def test_cache_file_is_private_to_the_user(tmp_path):
    path = str(tmp_path / "usage.json")
    plugin.write_cache(path, b'{"limits": []}')

    assert oct((tmp_path / "usage.json").stat().st_mode)[-3:] == "600"


# --- rate-limit backoff ---


def test_backoff_starts_far_above_the_poll_interval():
    # A pause shorter than the refresh cadence would keep the limit alive.
    assert plugin.backoff_seconds(1, None) >= 10 * 60


def test_backoff_widens_with_consecutive_refusals():
    delays = [plugin.backoff_seconds(n, None) for n in range(1, 6)]

    assert delays == sorted(delays)
    assert delays[-1] > delays[0]


def test_backoff_is_capped():
    assert plugin.backoff_seconds(50, None) == plugin.BACKOFF_MAX_SECONDS


@pytest.mark.parametrize("retry_after", [0, None, -5, "soon"])
def test_backoff_ignores_useless_server_hints(retry_after):
    # The endpoint has been observed answering "retry-after: 0"; obeying that
    # literally is what caused sustained throttling.
    assert plugin.backoff_seconds(1, retry_after) >= 10 * 60


def test_backoff_honours_a_longer_server_hint():
    assert plugin.backoff_seconds(1, 9999) == 9999


def test_backoff_state_round_trip(tmp_path):
    path = str(tmp_path / "backoff.json")
    plugin.write_backoff(path, 1234.5, 3)

    assert plugin.read_backoff(path) == (1234.5, 3)


def test_backoff_state_absent_or_corrupt_reads_as_clear(tmp_path):
    path = str(tmp_path / "backoff.json")
    assert plugin.read_backoff(path) == (0, 0)

    with open(path, "w") as handle:
        handle.write("{garbage")
    assert plugin.read_backoff(path) == (0, 0)


def test_clear_backoff_removes_state(tmp_path):
    path = str(tmp_path / "backoff.json")
    plugin.write_backoff(path, 1234.5, 3)
    plugin.clear_backoff(path)

    assert plugin.read_backoff(path) == (0, 0)
    # Clearing an already-clear state must not raise.
    plugin.clear_backoff(path)


# --- severity colouring of dropdown lines ---


def test_limit_lines_are_grey_when_stale():
    lines = plugin.render(fixture("exceeded"), NOW, "stale-fetch", "boom", NOW)
    limit_lines = [rendered for rendered in lines if rendered.startswith(("Session", "Weekly", "Fa/ble"))]

    assert limit_lines
    for rendered in limit_lines:
        assert "color=gray" in rendered
        # Stale data must never keep its alarm colour; that would read as live.
        assert "color=red" not in rendered


def test_limit_line_keeps_severity_colour_when_fresh():
    lines = plugin.render(fixture("exceeded"), NOW, "fresh", None, NOW)

    assert any(rendered.startswith("Fa/ble") and "color=red" in rendered for rendered in lines)


# --- hostile numeric payloads ---


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_percent_renders_dash_instead_of_crashing(literal):
    # json.loads accepts these non-standard literals, and int() rejects them.
    payload = json.loads('{"limits": [{"kind": "session", "percent": %s}]}' % literal)
    lines = plugin.render(payload, NOW, "fresh", None, NOW)

    assert title_text(lines) == "S-%"
    assert any(rendered.startswith("Session - ") for rendered in lines)


@pytest.mark.parametrize("state", ["fresh", "stale-fetch"])
@pytest.mark.parametrize(
    "limits",
    [[], [None, 3, "x"], [{"kind": "monthly_beta", "percent": 5}]],
)
def test_title_without_known_slots_is_a_readable_word(limits, state):
    lines = plugin.render({"limits": limits}, NOW, state, "reason", NOW)

    assert title_text(lines) == plugin.EMPTY_TITLE
    # SwiftBar treats a leading "--" as a submenu marker.
    assert not title_text(lines).startswith("--")


@pytest.mark.parametrize("limits", [[], [None, 3, "x"]])
def test_dropdown_says_so_when_no_limits_are_reported(limits):
    lines = plugin.render({"limits": limits}, NOW, "fresh", None, NOW)

    assert any(rendered.startswith("No usage limits reported") for rendered in lines)


def test_dropdown_stays_quiet_when_only_unknown_limits_are_reported():
    payload = {"limits": [{"kind": "monthly_beta", "percent": 5}]}
    lines = plugin.render(payload, NOW, "fresh", None, NOW)

    assert not any(rendered.startswith("No usage limits reported") for rendered in lines)
    assert any(rendered.startswith("Monthly Beta 5%") for rendered in lines)


# --- daylight saving correctness ---


def test_local_now_carries_daylight_saving_rules():
    # A plain astimezone() would freeze today's offset onto future instants.
    assert not isinstance(plugin.local_now().tzinfo, timezone)


def test_reset_across_dst_boundary_uses_the_offset_in_force_then():
    zoneinfo = pytest.importorskip("zoneinfo")
    sydney = zoneinfo.ZoneInfo("Australia/Sydney")
    # 4 Oct 2026 is the DST switch: +10:00 before, +11:00 after.
    now = datetime(2026, 10, 1, 12, 0, tzinfo=sydney)
    entry = {"kind": "session", "percent": 5, "resets_at": "2026-10-06T01:00:00+00:00"}

    rendered = plugin.limit_line(entry, now, False)

    # 01:00 UTC maps to 12:00 local at +11:00, not 11:00 at the stale offset.
    assert "resets Tue 12:00" in rendered
