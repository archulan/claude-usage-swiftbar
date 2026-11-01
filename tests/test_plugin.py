"""Contract tests for the pure rendering layer of claude-usage.5m.py.

The plugin filename is not an importable module name, so the module is loaded
from its file path. All time-dependent tests pin an aware "now" with a fixed
UTC+10 offset so results never depend on the machine's timezone.
"""

import importlib.util
import io
import json
import re
import socket
import urllib.error
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
    api_lines = [r for r in lines if r.startswith(("Session", "Weekly", "Fable", "Usage credits"))]

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

    assert rendered == "Usage credits: $0.00 / $50.00"


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

    assert not any("Usage credits:" in rendered for rendered in lines)


# --- unknown limit kinds ---


def test_unknown_kind_renders_generically_in_dropdown():
    lines = plugin.render(fixture("unknown"), NOW, "fresh", None, NOW)

    assert any(rendered.split(" | ")[0].startswith("Monthly Beta 55%") for rendered in lines)


# --- dollar allowances ---


def credits_block(**overrides):
    """Builds an allowance block shaped like the live cloud session credits."""
    block = {
        "utilization": 1.49,
        "resets_at": "2026-11-05T07:59:00+00:00",
        "limit_dollars": 250,
        "used_dollars": 3.73,
        "remaining_dollars": 246.27,
        "locked_reason": None,
    }
    block.update(overrides)
    return block


def allowance_text(block, key="iguana_necktie"):
    """Returns the text of an allowance row, without its parameters."""
    return plugin.allowance_line(key, block, ULTRA_NOW, False).split(" | ")[0]


def test_cloud_credits_row_shows_dollars_left_and_expiry():
    rendered = plugin.allowance_line("iguana_necktie", fixture("ultra")["iguana_necktie"], ULTRA_NOW, False)

    assert rendered == (
        "Cloud session credits: $246 of $250 left · expires Thu 18:59 (in 3d 9h)"
        " | emojize=false symbolize=false"
    )


def test_ultra_dropdown_lists_cloud_credits_after_the_limits():
    lines = plugin.render(fixture("ultra"), ULTRA_NOW, "fresh", None, ULTRA_NOW)
    labels = [rendered.split(" ")[0] for rendered in lines]

    assert labels.index("Cloud") == labels.index("Fable") + 1


@pytest.mark.parametrize(
    "block",
    [
        None,
        "x",
        [],
        {},
        credits_block(limit_dollars=None),
        credits_block(limit_dollars=0),
        credits_block(limit_dollars=-5),
        credits_block(limit_dollars=True),
        credits_block(limit_dollars=float("nan")),
        credits_block(limit_dollars="250"),
        credits_block(remaining_dollars=None, used_dollars=None),
        # Blocks that track a limit as a percentage share this shape.
        {"utilization": 0.0, "resets_at": None, "limit_dollars": None, "remaining_dollars": None},
    ],
)
def test_allowance_skipped_when_null_or_malformed(block):
    assert plugin.dollar_allowances({"copper_kite": block}) == []


def test_remaining_dollars_derived_from_used_when_absent():
    block = credits_block(remaining_dollars=None, used_dollars=3.73)

    assert allowance_text(block).startswith("Cloud session credits: $246 of $250 left")


@pytest.mark.parametrize(
    "overrides",
    [{"remaining_dollars": -5}, {"remaining_dollars": None, "used_dollars": 300}],
)
def test_remaining_dollars_never_goes_below_zero(overrides):
    assert allowance_text(credits_block(**overrides)).startswith("Cloud session credits: $0 of $250 left")


def test_exhausted_allowance_has_no_colour_and_no_title_symbol():
    payload = dict(fixture("ultra"), iguana_necktie=credits_block(remaining_dollars=0))
    lines = plugin.render(payload, ULTRA_NOW, "fresh", None, ULTRA_NOW)
    row = next(rendered for rendered in lines if rendered.startswith("Cloud session credits"))

    assert "color=" not in row
    assert "sfimage=" not in title_of(lines)


def test_expired_allowance_reads_expired_in_grey():
    block = credits_block(resets_at="2026-11-01T00:00:00+00:00")
    rendered = plugin.allowance_line("iguana_necktie", block, ULTRA_NOW, False)

    assert rendered.split(" | ")[0] == "Cloud session credits: expired"
    assert "color=gray" in rendered


@pytest.mark.parametrize("value", [None, "garbage"])
def test_expiry_clause_dropped_when_resets_at_missing_or_garbage(value):
    assert allowance_text(credits_block(resets_at=value)) == "Cloud session credits: $246 of $250 left"


def test_allowance_row_is_grey_when_stale():
    rendered = plugin.allowance_line("iguana_necktie", credits_block(), ULTRA_NOW, True)

    assert "color=gray" in rendered


@pytest.mark.parametrize("key", ["five_hour", "seven_day", "seven_day_opus", "spend", "extra_usage"])
def test_blocks_covered_by_other_rows_are_not_allowances(key):
    assert plugin.dollar_allowances({key: credits_block()}) == []


def test_unknown_codename_gets_a_generic_label_and_the_literal_field_name():
    block = credits_block(limit_dollars=10, remaining_dollars=4)

    assert allowance_text(block, "copper_kite") == "Copper Kite: $4 of $10 left · resets Thu 18:59 (in 3d 9h)"


def test_allowances_list_known_keys_first_then_by_key():
    payload = {"zebra": credits_block(), "apple": credits_block(), "iguana_necktie": credits_block()}

    assert [key for key, _ in plugin.dollar_allowances(payload)] == ["iguana_necktie", "apple", "zebra"]


def test_hostile_allowance_key_keeps_the_line_parseable():
    assert_first_pipe_is_the_separator(plugin.allowance_line("a|b", credits_block(), ULTRA_NOW, False))


@pytest.mark.parametrize(
    "value,expected",
    [(246.27, "$246"), (246.99, "$246"), (250, "$250"), (1000, "$1,000"), (0.4, "$0"), (0, "$0")],
)
def test_dollars_render_as_whole_dollars_rounded_down(value, expected):
    assert plugin.format_dollars(value) == expected


@pytest.mark.parametrize(
    "value,expected",
    [
        (3, 3.0),
        (2.5, 2.5),
        (True, None),
        ("3", None),
        (None, None),
        (float("nan"), None),
        (float("inf"), None),
        # json.loads builds arbitrarily large ints, which overflow float().
        (10 ** 400, None),
    ],
)
def test_finite_number_rejects_everything_but_usable_numbers(value, expected):
    assert plugin.finite_number(value) == expected


def test_oversized_percent_renders_dash_instead_of_crashing():
    assert plugin.percent_text({"percent": 10 ** 400}) == "-"


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


# --- authenticated requests ---


class FailingOpener:
    """Stands in for a urllib opener whose every request raises."""

    def __init__(self, error):
        self.error = error

    def open(self, request, timeout=None):
        raise self.error


def http_error(code):
    # Without a body file, HTTPError's attribute lookup raises on Python 3.9.
    return urllib.error.HTTPError("https://example.test", code, "refused", None, io.BytesIO())


@pytest.mark.parametrize(
    "error,reason,rate_limited",
    [
        (http_error(429), "rate limited by the API", True),
        (http_error(500), "thing request failed (HTTP 500)", False),
        (socket.timeout(), "thing request timed out", False),
        (urllib.error.URLError(socket.timeout()), "thing request timed out", False),
        (urllib.error.URLError("refused"), "network error", False),
        (OSError("reset"), "network error", False),
    ],
)
def test_fetch_json_names_the_request_in_its_failure_reason(monkeypatch, error, reason, rate_limited):
    monkeypatch.setattr(plugin.urllib.request, "build_opener", lambda *_: FailingOpener(error))

    assert plugin.fetch_json("https://example.test", "token", "thing") == (None, reason, rate_limited)


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
