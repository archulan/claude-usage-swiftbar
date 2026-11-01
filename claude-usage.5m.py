#!/usr/bin/env python3
# <xbar.title>Claude Usage</xbar.title>
# <xbar.version>v1.1</xbar.version>
# <xbar.author>Archulan</xbar.author>
# <xbar.author.github>archulan</xbar.author.github>
# <xbar.desc>Shows claude.ai subscription usage limits (session where your plan has one, weekly, per-model) with reset times, your plan and included credits.</xbar.desc>
# <xbar.dependencies>python3</xbar.dependencies>
# <xbar.abouturl>https://github.com/archulan/claude-usage-swiftbar</xbar.abouturl>
# <swiftbar.hideRunInTerminal>true</swiftbar.hideRunInTerminal>
# <swiftbar.refreshOnOpen>true</swiftbar.refreshOnOpen>

"""SwiftBar plugin showing claude.ai subscription usage limits in the menu bar.

Reads the Claude Code OAuth access token (Keychain, with a file fallback) and
queries the same usage endpoint the official clients use, plus the profile
endpoint once a day for the plan name. Rendering is kept as pure functions over
the parsed payload so the behaviour is testable without touching the network or
the Keychain.
"""

import http.client
import json
import math
import os
import socket
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"
USAGE_PAGE_URL = "https://claude.ai/settings/usage"
OAUTH_BETA = "oauth-2025-04-20"
KEYCHAIN_SERVICE = "Claude Code-credentials"
CREDS_FILE = "~/.claude/.credentials.json"
HTTP_TIMEOUT = 10

# The usage endpoint is shared with the official clients and throttles well
# below a per-minute cadence, so a refusal is answered with a widening pause
# rather than another attempt on the next tick.
BACKOFF_BASE_SECONDS = 15 * 60
BACKOFF_MAX_SECONDS = 4 * 60 * 60

# A plan rarely changes, so a daily lookup keeps the poll at one request per
# tick against the same narrow rate limit.
PROFILE_TTL_SECONDS = 24 * 60 * 60
PROFILE_RETRY_SECONDS = 60 * 60

# Unknown severities rank as warning: a value this plugin has never seen most
# likely means the account is in some flavour of trouble, not that all is well.
SEVERITY_RANK = {"normal": 0, "warning": 1, "elevated": 1, "exceeded": 2, "critical": 2}

SEVERITY_COLOR = {1: "orange", 2: "red"}

CURRENCY_SYMBOL = {"USD": "$", "AUD": "A$", "EUR": "€", "GBP": "£"}

# Display order of the limit kinds with a menu bar slot. Plans differ in which
# of these they have, so only the kinds the API reports are shown.
TITLE_SLOTS = ("session", "weekly_all", "weekly_scoped")

# A word rather than an empty string, which would hide the menu bar item.
EMPTY_TITLE = "Claude"

# Dollar allowances sit under opaque keys, so the labels the settings page uses
# are mapped by hand as (label, expiry verb), in display order.
KNOWN_ALLOWANCES = {"iguana_necktie": ("Cloud session credits", "expires")}

# Blocks with the allowance shape that other rows already cover, matched
# exactly or as a prefix so that seven_day_opus and the like are caught too.
ALLOWANCE_EXCLUDED = ("five_hour", "seven_day", "spend", "extra_usage")


# --- pure rendering layer ---


def sanitize(text):
    """Makes arbitrary text safe for a SwiftBar line.

    SwiftBar splits every output line at the first literal pipe, so a pipe
    inside dynamic text (a model name, an error reason) would silently truncate
    the line. Newlines would likewise start a new menu item.
    """
    return " ".join(str(text).replace("|", "/").split())


def line(text, **params):
    """Builds one SwiftBar output line; the single choke point for pipe safety."""
    rendered = sanitize(text)
    parts = []

    for key, value in params.items():
        if value is None:
            continue
        value = sanitize(value)
        if " " in value:
            value = '"' + value + '"'
        parts.append(key + "=" + value)

    if parts:
        return rendered + " | " + " ".join(parts)
    return rendered


def untrusted_line(text, **params):
    """Builds a line whose text came from the API.

    Shortcode expansion is switched off so a model named ":fire:" renders as
    that text rather than turning into a picture.
    """
    return line(text, emojize="false", symbolize="false", **params)


def severity_rank(severity):
    """Maps a severity string to 0 (normal), 1 (warning) or 2 (exceeded)."""
    if severity is None:
        return 0
    if not isinstance(severity, str):
        return 1
    return SEVERITY_RANK.get(severity.lower(), 1)


def classify_limits(limits):
    """Splits the limits array into the three known slots plus leftovers.

    First entry per known kind wins a slot; duplicates and unknown kinds are
    kept as extras so new limit types the API grows still show in the dropdown
    instead of vanishing.
    """
    slots = {}
    extras = []

    if not isinstance(limits, list):
        return slots, extras

    for entry in limits:
        if not isinstance(entry, dict):
            continue
        kind = entry.get("kind")
        if kind in TITLE_SLOTS and kind not in slots:
            slots[kind] = entry
        else:
            extras.append(entry)

    return slots, extras


def scoped_model_name(entry):
    """Returns the model name a scoped limit applies to, or None."""
    if not isinstance(entry, dict):
        return None

    scope = entry.get("scope")
    model = scope.get("model") if isinstance(scope, dict) else None
    name = model.get("display_name") if isinstance(model, dict) else None

    if isinstance(name, str) and name.strip():
        return sanitize(name)
    return None


def finite_number(value):
    """Returns value as a float, or None when it is unusable as a number.

    json.loads accepts NaN and Infinity, and builds ints too large for float().
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def limit_label(entry):
    """Returns the display name for a limit entry."""
    kind = entry.get("kind")

    if kind == "session":
        return "Session"
    if kind == "weekly_all":
        return "Weekly (all models)"
    if kind == "weekly_scoped":
        return scoped_model_name(entry) or "Weekly (model)"
    if isinstance(kind, str) and kind:
        return sanitize(kind.replace("_", " ").title())
    return "Limit"


def percent_text(entry):
    """Returns the whole-number percent for an entry, or a dash when unusable."""
    if not isinstance(entry, dict):
        return "-"
    percent = finite_number(entry.get("percent"))
    if percent is None:
        return "-"
    return str(int(round(percent)))


def parse_reset(value):
    """Parses an ISO 8601 reset timestamp into an aware datetime, or None.

    Python 3.9's fromisoformat rejects a trailing Z, and a naive result would
    poison later arithmetic against aware datetimes, so both are normalised.
    """
    if not isinstance(value, str) or not value:
        return None

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def format_countdown(reset, now):
    """Formats the time until reset in the coarsest useful unit pair."""
    total = int((reset - now).total_seconds())

    if total <= 0:
        return "soon"
    if total < 60:
        return "in <1m"

    minutes, _ = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)

    if days >= 7:
        return "in {}d".format(days)
    if days:
        return "in {}d {}h".format(days, hours) if hours else "in {}d".format(days)
    if hours:
        return "in {}h {}m".format(hours, minutes) if minutes else "in {}h".format(hours)
    return "in {}m".format(minutes)


def format_moment(moment, now):
    """Formats an instant in the viewer's timezone, tersest useful form first.

    A weekday alone is ambiguous beyond a week either side of now, so anything
    that far out gains a date.
    """
    local = moment.astimezone(now.tzinfo)

    if local.date() == now.date():
        return local.strftime("%H:%M")
    if abs(moment - now) < timedelta(days=7):
        return local.strftime("%a %H:%M")
    # %-d is platform specific, so the unpadded day is built manually.
    return "{} {}".format(local.day, local.strftime("%b %H:%M"))


def resets_clause(value, now, verb="resets"):
    """Returns the "resets ..." fragment for a line, or None to omit it."""
    reset = parse_reset(value)

    if reset is None:
        return None
    if reset <= now:
        # A reset in the past just means the API has not rolled the window
        # yet; the next refresh self-heals, so no alarm styling.
        return verb + " soon"
    return "{} {} ({})".format(verb, format_moment(reset, now), format_countdown(reset, now))


def limit_line(entry, now, stale):
    """Renders one dropdown line for a limit entry."""
    percent = percent_text(entry)
    text = limit_label(entry) + " " + (percent + "%" if percent != "-" else "-")

    clause = resets_clause(entry.get("resets_at"), now)
    if clause:
        text += " · " + clause

    color = "gray" if stale else SEVERITY_COLOR.get(severity_rank(entry.get("severity")))
    return untrusted_line(text, color=color)


def _money_block_ok(block):
    """Validates the amount/exponent shape of a spend money block."""
    return (
        isinstance(block, dict)
        and isinstance(block.get("amount_minor"), (int, float))
        and not isinstance(block.get("amount_minor"), bool)
        and isinstance(block.get("exponent"), int)
        and 0 <= block["exponent"] <= 4
    )


def format_money(amount_minor, exponent, currency):
    """Formats a minor-unit amount, preferring a symbol over a code suffix."""
    value = amount_minor / (10 ** exponent)
    text = "{:.{}f}".format(value, exponent)

    if not isinstance(currency, str):
        return text
    symbol = CURRENCY_SYMBOL.get(currency)
    if symbol:
        return symbol + text
    return text + " " + sanitize(currency)


def spend_line(spend):
    """Renders the usage-credits line, or None when there is nothing to show."""
    if not isinstance(spend, dict) or not spend.get("enabled"):
        return None

    used = spend.get("used")
    limit = spend.get("limit")
    if not _money_block_ok(used) or not _money_block_ok(limit):
        return None

    return "Usage credits: {} / {}".format(
        format_money(used["amount_minor"], used["exponent"], used.get("currency")),
        format_money(limit["amount_minor"], limit["exponent"], limit.get("currency")),
    )


def format_dollars(value):
    """Formats dollars whole, rounded down so a balance is never overstated."""
    return "${:,}".format(math.floor(value))


def is_limit_mirror(key):
    """Checks whether a payload key is already covered by another dropdown row."""
    return any(key == name or key.startswith(name + "_") for name in ALLOWANCE_EXCLUDED)


def dollar_allowances(payload):
    """Returns (key, block) for each dollar allowance in the payload.

    Allowances sit under opaque keys that change without notice, so they are
    found by shape: a positive dollar limit and a usable balance. Keys with a
    known label come first, in the order the settings page uses.
    """
    found = []

    for key, block in payload.items():
        if is_limit_mirror(key) or not isinstance(block, dict):
            continue
        limit = finite_number(block.get("limit_dollars"))
        if limit is None or limit <= 0:
            continue
        if finite_number(block.get("remaining_dollars")) is None and finite_number(block.get("used_dollars")) is None:
            continue
        found.append((key, block))

    known = list(KNOWN_ALLOWANCES)
    found.sort(key=lambda item: (known.index(item[0]) if item[0] in known else len(known), item[0]))
    return found


def allowance_terms(key):
    """Returns (label, expiry verb) for an allowance key.

    An unknown key says "resets", the literal field name, because what the
    date means for it is not known.
    """
    return KNOWN_ALLOWANCES.get(key) or (sanitize(key.replace("_", " ").title()), "resets")


def allowance_line(key, block, now, stale):
    """Renders one dropdown line for a dollar allowance.

    The line never carries a severity colour: running out of an allowance
    falls back to the plan's regular limits, so it is not an alarm.
    """
    label, verb = allowance_terms(key)
    resets_at = block.get("resets_at")
    expiry = parse_reset(resets_at)

    if verb == "expires" and expiry is not None and expiry <= now:
        return untrusted_line(label + ": expired", color="gray")

    limit = finite_number(block.get("limit_dollars"))
    remaining = finite_number(block.get("remaining_dollars"))
    if remaining is None:
        remaining = limit - finite_number(block.get("used_dollars"))

    text = "{}: {} of {} left".format(label, format_dollars(max(remaining, 0)), format_dollars(limit))

    clause = resets_clause(resets_at, now, verb)
    if clause:
        text += " · " + clause

    return untrusted_line(text, color="gray" if stale else None)


def extract_plan_label(profile):
    """Returns the plan's display text from a profile document, or None.

    The rate limit tier is deliberately not consulted: it keeps internal names
    that lag behind the plan names customers see.
    """
    organization = profile.get("organization") if isinstance(profile, dict) else None
    if not isinstance(organization, dict):
        return None

    for field in ("plan_display_label", "plan_display_name"):
        value = organization.get(field)
        if isinstance(value, str) and value.strip():
            return sanitize(value)
    return None


def slot_initial(kind, entry):
    """Returns the one-letter tag naming a slot in the menu bar title.

    The scoped slot takes its letter from whatever model the API reports, so a
    rename upstream carries through without a code change.
    """
    if kind == "session":
        return "S"
    if kind == "weekly_all":
        return "W"

    # "M" for an unnamed model, which also avoids colliding with the weekly
    # slot's letter when the generic label would start with the same one.
    name = scoped_model_name(entry)
    if name and name[0].isalnum():
        return name[0].upper()
    return "M"


def build_title(slots, worst, state):
    """Builds the menu bar title line for the given freshness state.

    The title carries no colour. The menu bar is translucent, so whatever the
    wallpaper puts behind it sets the real contrast, and text colour is
    resolved from the system light/dark setting, which cannot see that
    backdrop. Symbols are resolved against the status bar itself and stay
    legible, so state and severity are carried by a symbol and the digits keep
    the system colour that always adapts.
    """
    tagged = [slot_initial(kind, slots[kind]) + percent_text(slots[kind]) for kind in TITLE_SLOTS if kind in slots]
    text = "·".join(tagged) + "%" if tagged else EMPTY_TITLE

    if state == "stale-token":
        return line(text, sfimage="clock")
    if state == "stale-fetch":
        return line(text, sfimage="exclamationmark.triangle")
    if state == "error":
        return line("", sfimage="exclamationmark.triangle")
    if worst >= 1:
        return line(text, sfimage="exclamationmark.triangle")
    return line(text)


def format_updated(fetched_at, now):
    """Formats the snapshot age for the dropdown footer."""
    return format_moment(fetched_at, now)


def render(payload, now, state, reason, fetched_at, plan=None):
    """Renders the full SwiftBar output as a list of lines.

    state is one of fresh, stale-token, stale-fetch, error; payload is the
    parsed usage response (live or cached) or None when nothing is renderable;
    plan is the plan label, or None when it is not known.
    """
    stale = state != "fresh"
    limits = payload.get("limits") if isinstance(payload, dict) else None
    slots, extras = classify_limits(limits)

    worst = 0
    for entry in list(slots.values()) + extras:
        worst = max(worst, severity_rank(entry.get("severity")))

    lines = [build_title(slots, worst, state), "---"]

    if plan:
        lines.append(untrusted_line(plan))

    if isinstance(payload, dict):
        reported = [slots[kind] for kind in TITLE_SLOTS if kind in slots] + extras
        for entry in reported:
            lines.append(limit_line(entry, now, stale))
        if not reported:
            lines.append(line("No usage limits reported", color="gray"))

        for key, block in dollar_allowances(payload):
            lines.append(allowance_line(key, block, now, stale))

        credits = spend_line(payload.get("spend"))
        if credits:
            lines.append(untrusted_line(credits, color="gray" if stale else None))

    if reason:
        lines.append(line("⚠ " + reason, color="gray"))

    lines.append("---")
    lines.append(line("Open claude.ai usage", href=USAGE_PAGE_URL))

    updated = "never" if fetched_at is None else format_updated(fetched_at, now)
    if stale and fetched_at is not None:
        updated += " (stale)"
    lines.append(line("Updated " + updated, color="gray", size=10))

    lines.append(line("Refresh", refresh="true"))
    return lines


# --- I/O layer ---


def read_credentials():
    """Reads the Claude Code OAuth access token and its expiry.

    Returns (token, expires_at_ms, error_reason). The Keychain secret arrives
    on a stdout pipe, never argv, so the token is not visible in the process
    list. Only accessToken and expiresAt are read; the refresh token is left
    untouched so this plugin can never interfere with Claude Code's session.
    Error reasons are canned strings: raw parse errors could echo secret
    material into the menu.
    """
    raw = None

    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            raw = result.stdout
    except (OSError, subprocess.SubprocessError):
        raw = None

    if raw is None:
        try:
            with open(os.path.expanduser(CREDS_FILE)) as handle:
                raw = handle.read()
        except OSError:
            return None, None, "Claude Code credentials not found"

    try:
        oauth = json.loads(raw).get("claudeAiOauth")
        token = oauth.get("accessToken")
        expires_at_ms = oauth.get("expiresAt")
    except (ValueError, AttributeError):
        return None, None, "credentials unreadable"

    if not isinstance(token, str) or not token:
        return None, None, "credentials unreadable"
    return token, expires_at_ms, None


def token_is_fresh(expires_at_ms, now):
    """Checks the token expiry with a small margin for clock skew."""
    if isinstance(expires_at_ms, bool) or not isinstance(expires_at_ms, (int, float)):
        return False
    return expires_at_ms / 1000.0 > now.timestamp() + 60


class _BlockRedirects(urllib.request.HTTPRedirectHandler):
    """Refuses to follow redirects so the bearer token is never replayed.

    urllib copies the original request headers onto a redirect target, which
    would hand the OAuth token to whatever host the redirect names. The one
    endpoint this plugin talks to has no legitimate reason to redirect.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch_json(url, token, what):
    """Fetches an API document; returns (body_bytes, error_reason, rate_limited).

    The request is made in-process so the token only ever exists in memory and
    the Authorization header. socket.timeout is caught explicitly because it
    only merges into TimeoutError in Python 3.10, and HTTPException because
    urlopen only wraps OSError into URLError.
    """
    request = urllib.request.Request(
        url,
        headers={"Authorization": "Bearer " + token, "anthropic-beta": OAUTH_BETA},
    )
    opener = urllib.request.build_opener(_BlockRedirects)

    try:
        with opener.open(request, timeout=HTTP_TIMEOUT) as response:
            return response.read(), None, False
    except urllib.error.HTTPError as error:
        if error.code == 429:
            return None, "rate limited by the API", True
        return None, "{} request failed (HTTP {})".format(what, error.code), False
    except socket.timeout:
        return None, "{} request timed out".format(what), False
    except urllib.error.URLError as error:
        if isinstance(getattr(error, "reason", None), socket.timeout):
            return None, "{} request timed out".format(what), False
        return None, "network error", False
    except (OSError, http.client.HTTPException):
        return None, "network error", False


def fetch_usage(token):
    """Fetches the usage payload; returns (body_bytes, error_reason, rate_limited)."""
    return fetch_json(USAGE_URL, token, "usage")


def fetch_profile(token):
    """Fetches the profile document; returns (body_bytes, error_reason, rate_limited)."""
    return fetch_json(PROFILE_URL, token, "profile")


def cache_dir():
    """Returns the state directory, preferring SwiftBar's per-plugin cache."""
    base = os.environ.get("SWIFTBAR_PLUGIN_CACHE_PATH")
    if base:
        return base
    return os.path.expanduser("~/.cache/claude-usage")


def cache_file():
    """Returns the usage snapshot path."""
    return os.path.join(cache_dir(), "usage.json")


def backoff_file():
    """Returns the path holding the rate-limit backoff state."""
    return os.path.join(cache_dir(), "backoff.json")


def profile_file():
    """Returns the path holding the cached plan label."""
    return os.path.join(cache_dir(), "profile.json")


def backoff_seconds(consecutive, retry_after):
    """Returns how long to wait after a rate-limit response.

    The server's own hint is treated as a lower bound only: it has been
    observed returning 0, and obeying that literally is what turns a brief
    throttle into a sustained one. Repeated refusals widen the gap so a
    persistent limit is not met with a constant knock.
    """
    delay = BACKOFF_BASE_SECONDS * (2 ** max(0, consecutive - 1))
    delay = min(delay, BACKOFF_MAX_SECONDS)

    if isinstance(retry_after, (int, float)) and not isinstance(retry_after, bool):
        delay = max(delay, int(retry_after))
    return delay


def read_backoff(path):
    """Returns (blocked_until_epoch, consecutive_failures); zeros when absent."""
    try:
        with open(path) as handle:
            state = json.load(handle)
        until = state.get("until")
        consecutive = state.get("consecutive")
        if not isinstance(until, (int, float)) or not isinstance(consecutive, int):
            return 0, 0
        return until, consecutive
    except (OSError, ValueError, AttributeError):
        return 0, 0


def write_backoff(path, until, consecutive):
    """Records the backoff deadline, best effort."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as handle:
            json.dump({"until": until, "consecutive": consecutive}, handle)
    except OSError:
        pass


def clear_backoff(path):
    """Drops the backoff state after a successful fetch."""
    try:
        os.unlink(path)
    except OSError:
        pass


def next_profile_state(previous, body, rate_limited, now_ts):
    """Returns (plan, next_attempt_epoch) after a profile fetch.

    A failed fetch keeps the previous label, since a stale plan name is better
    than none. Only an ordinary failure retries early: a refusal or a document
    without a label would not improve within the hour.
    """
    if body is None and not rate_limited:
        return previous, now_ts + PROFILE_RETRY_SECONDS

    plan = None
    if body is not None:
        try:
            plan = extract_plan_label(json.loads(body))
        except ValueError:
            plan = None
    return plan or previous, now_ts + PROFILE_TTL_SECONDS


def read_profile(path):
    """Returns (plan, next_attempt_epoch); (None, 0) means unknown and due."""
    try:
        with open(path) as handle:
            state = json.load(handle)
        plan = state.get("plan")
        next_attempt = finite_number(state.get("next_attempt"))
    except (OSError, ValueError, AttributeError):
        return None, 0

    if next_attempt is None:
        return None, 0
    return (plan if isinstance(plan, str) else None), next_attempt


def write_profile(path, plan, next_attempt):
    """Records the plan label and next fetch time, best effort.

    Only these two values are kept because the full profile carries the
    account's name and email address.
    """
    _write_private(path, json.dumps({"plan": plan, "next_attempt": next_attempt}).encode())


def refresh_plan(token, plan, next_attempt, now_ts):
    """Refreshes the cached plan label when due; returns the label to show."""
    if now_ts < next_attempt:
        return plan

    body, _, rate_limited = fetch_profile(token)
    plan, next_attempt = next_profile_state(plan, body, rate_limited, now_ts)
    write_profile(profile_file(), plan, next_attempt)

    if rate_limited:
        # Same host as the usage endpoint, so the throttle is assumed shared.
        write_backoff(backoff_file(), now_ts + backoff_seconds(1, None), 1)
    return plan


def is_usage_payload(payload):
    """Checks a response actually carries usage data.

    A 200 whose body is an error envelope or an unrelated document is still a
    dict, and accepting one would overwrite a good snapshot with something
    that renders as all dashes.
    """
    return isinstance(payload, dict) and isinstance(payload.get("limits"), list)


def write_cache(path, raw):
    """Stores the raw response as the last-known-good snapshot, best effort.

    The body is validated before writing so a bad response can never destroy a
    good snapshot.
    """
    try:
        if not is_usage_payload(json.loads(raw)):
            return
    except ValueError:
        return

    _write_private(path, raw)


def _write_private(path, raw):
    """Replaces a file atomically, readable by the user only, best effort.

    The temp file lives in the destination directory because os.replace is only
    atomic within one filesystem.
    """
    directory = os.path.dirname(path)
    tmp_path = None
    try:
        os.makedirs(directory, exist_ok=True)
        descriptor, tmp_path = tempfile.mkstemp(dir=directory)
        try:
            os.write(descriptor, raw)
        finally:
            os.close(descriptor)
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
        tmp_path = None
    except OSError:
        pass
    finally:
        # Without this a persistent replace failure would litter the cache
        # directory with one abandoned temp file per run.
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def read_cache(path):
    """Loads the cached snapshot; returns (payload, fetched_at) or (None, None).

    The file's mtime is the freshness record, avoiding an envelope format.
    """
    try:
        with open(path) as handle:
            payload = json.load(handle)
            # Stat the open handle, not the path: a concurrent refresh could
            # replace the file between the read and a second lookup by name.
            fetched_at = datetime.fromtimestamp(os.fstat(handle.fileno()).st_mtime, timezone.utc)
        if not isinstance(payload, dict):
            return None, None
        return payload, fetched_at
    except (OSError, ValueError):
        return None, None


def local_now():
    """Returns the current time in a rule-aware local timezone where possible.

    astimezone() alone yields a fixed offset frozen at today's value, which
    would label a reset on the far side of a daylight saving change with an
    offset that is not in force at that instant.
    """
    now = datetime.now(timezone.utc)

    try:
        import zoneinfo

        key = os.path.realpath("/etc/localtime").split("zoneinfo/")[-1]
        return now.astimezone(zoneinfo.ZoneInfo(key))
    except Exception:
        return now.astimezone()


def main():
    now = local_now()
    path = cache_file()
    backoff_path = backoff_file()
    blocked_until, consecutive = read_backoff(backoff_path)
    plan, next_profile_attempt = read_profile(profile_file())
    token, expires_at_ms, cred_reason = read_credentials()

    if now.timestamp() < blocked_until:
        # Holding off entirely is the point: another request here is what
        # would keep the limit alive.
        resume = datetime.fromtimestamp(blocked_until, timezone.utc)
        reason = "rate limited, resuming {}".format(format_moment(resume, now))
        state = "stale-fetch"
    elif token is not None and token_is_fresh(expires_at_ms, now):
        body, fetch_reason, rate_limited = fetch_usage(token)

        payload = None
        if body is not None:
            try:
                parsed = json.loads(body)
                payload = parsed if is_usage_payload(parsed) else None
            except ValueError:
                payload = None

        if payload is not None:
            write_cache(path, body)
            clear_backoff(backoff_path)
            plan = refresh_plan(token, plan, next_profile_attempt, now.timestamp())
            print("\n".join(render(payload, now, "fresh", None, now, plan=plan)))
            return

        if rate_limited:
            consecutive += 1
            resume_at = now.timestamp() + backoff_seconds(consecutive, None)
            write_backoff(backoff_path, resume_at, consecutive)
            resume = datetime.fromtimestamp(resume_at, timezone.utc)
            fetch_reason = "rate limited, resuming {}".format(format_moment(resume, now))

        reason = fetch_reason or "usage response unreadable"
        state = "stale-fetch"
    else:
        # Deliberately passive: refreshing the token here could rotate the
        # refresh token out from under Claude Code and log it out.
        reason = cred_reason or "token expired, open Claude Code to refresh"
        state = "stale-token"

    cached, fetched_at = read_cache(path)
    if cached is not None:
        print("\n".join(render(cached, now, state, reason, fetched_at, plan=plan)))
    else:
        print("\n".join(render(None, now, "error", reason, None, plan=plan)))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # A crash would blank the menu bar item entirely; an error glyph with
        # a generic reason is strictly more useful, so swallow everything.
        print("\n".join(render(None, datetime.now(timezone.utc), "error", "plugin error", None)))
        sys.stderr.write("claude-usage: unhandled error, see plugin output\n")
    sys.exit(0)
