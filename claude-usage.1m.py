#!/usr/bin/env python3
# <xbar.title>Claude Usage</xbar.title>
# <xbar.version>v1.0</xbar.version>
# <xbar.author>Archulan</xbar.author>
# <xbar.author.github>archulan</xbar.author.github>
# <xbar.desc>Shows claude.ai subscription usage limits (session, weekly, per-model) with reset times.</xbar.desc>
# <xbar.dependencies>python3</xbar.dependencies>
# <xbar.abouturl>https://github.com/archulan/claude-usage-swiftbar</xbar.abouturl>
# <swiftbar.hideRunInTerminal>true</swiftbar.hideRunInTerminal>
# <swiftbar.refreshOnOpen>true</swiftbar.refreshOnOpen>

"""SwiftBar plugin showing claude.ai subscription usage limits in the menu bar.

Reads the Claude Code OAuth access token (Keychain, with a file fallback) and
queries the same usage endpoint the official clients use. Rendering is kept as
pure functions over the parsed payload so the behaviour is testable without
touching the network or the Keychain.
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
USAGE_PAGE_URL = "https://claude.ai/settings/usage"
OAUTH_BETA = "oauth-2025-04-20"
KEYCHAIN_SERVICE = "Claude Code-credentials"
CREDS_FILE = "~/.claude/.credentials.json"
HTTP_TIMEOUT = 10

# Unknown severities rank as warning: a value this plugin has never seen most
# likely means the account is in some flavour of trouble, not that all is well.
SEVERITY_RANK = {"normal": 0, "warning": 1, "elevated": 1, "exceeded": 2, "critical": 2}

SEVERITY_COLOR = {1: "orange", 2: "red"}

CURRENCY_SYMBOL = {"USD": "$", "AUD": "A$", "EUR": "€", "GBP": "£"}

# The three limit kinds the claude.ai settings page renders, in display order.
TITLE_SLOTS = ("session", "weekly_all", "weekly_scoped")


# --- pure rendering layer ---


def sanitize(text):
    """Makes arbitrary text safe for a SwiftBar line.

    SwiftBar splits every output line at the first literal pipe, so a pipe
    inside dynamic text (a model name, an error reason) would silently truncate
    the line. Newlines would likewise start a new menu item, and a leading
    double dash would demote the line into a submenu of the one above it.
    """
    rendered = " ".join(str(text).replace("|", "/").split())

    if rendered.startswith("--"):
        # Non-breaking hyphens look identical but are not the submenu marker.
        rendered = "‑‑" + rendered[2:]
    return rendered


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


def limit_label(entry):
    """Returns the display name for a limit entry."""
    kind = entry.get("kind")

    if kind == "session":
        return "Session"
    if kind == "weekly_all":
        return "Weekly (all models)"
    if kind == "weekly_scoped":
        scope = entry.get("scope")
        model = scope.get("model") if isinstance(scope, dict) else None
        name = model.get("display_name") if isinstance(model, dict) else None
        if isinstance(name, str) and name.strip():
            return sanitize(name)
        return "Weekly (model)"
    if isinstance(kind, str) and kind:
        return sanitize(kind.replace("_", " ").title())
    return "Limit"


def percent_text(entry):
    """Returns the whole-number percent for an entry, or a dash when unusable."""
    if not isinstance(entry, dict):
        return "-"
    percent = entry.get("percent")
    if isinstance(percent, bool) or not isinstance(percent, (int, float)):
        return "-"
    # json.loads accepts NaN and Infinity, both of which explode in int().
    if not math.isfinite(percent):
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


def resets_clause(value, now):
    """Returns the "resets ..." fragment for a limit line, or None to omit it."""
    reset = parse_reset(value)

    if reset is None:
        return None
    if reset <= now:
        # A reset in the past just means the API has not rolled the window
        # yet; the next refresh self-heals, so no alarm styling.
        return "resets soon"
    return "resets {} ({})".format(format_moment(reset, now), format_countdown(reset, now))


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

    return "Credits: {} / {}".format(
        format_money(used["amount_minor"], used["exponent"], used.get("currency")),
        format_money(limit["amount_minor"], limit["exponent"], limit.get("currency")),
    )


def build_title(slots, worst, state):
    """Builds the menu bar title line for the given freshness state.

    Titles get a single named colour at most: light/dark colour pairs resolve
    against the system theme rather than the actual status bar background, so
    a pair can render illegibly. Uncoloured text always adapts.
    """
    digits = "·".join(percent_text(slots.get(kind)) for kind in TITLE_SLOTS) + "%"

    if state == "fresh":
        return line(digits, color=SEVERITY_COLOR.get(worst))
    if state == "stale-token":
        return line(digits, color="gray", sfimage="clock")
    if state == "stale-fetch":
        return line(digits, color="gray", sfimage="exclamationmark.triangle")
    return line("", color="gray", sfimage="exclamationmark.triangle")


def format_updated(fetched_at, now):
    """Formats the snapshot age for the dropdown footer."""
    return format_moment(fetched_at, now)


def render(payload, now, state, reason, fetched_at):
    """Renders the full SwiftBar output as a list of lines.

    state is one of fresh, stale-token, stale-fetch, error; payload is the
    parsed usage response (live or cached) or None when nothing is renderable.
    """
    stale = state != "fresh"
    limits = payload.get("limits") if isinstance(payload, dict) else None
    slots, extras = classify_limits(limits)

    worst = 0
    for entry in list(slots.values()) + extras:
        worst = max(worst, severity_rank(entry.get("severity")))

    lines = [build_title(slots, worst, state), "---"]

    if isinstance(payload, dict):
        for kind in TITLE_SLOTS:
            lines.append(limit_line(slots.get(kind, {"kind": kind}), now, stale))
        for entry in extras:
            lines.append(limit_line(entry, now, stale))

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


def fetch_usage(token):
    """Fetches the usage payload; returns (body_bytes, error_reason).

    The request is made in-process so the token only ever exists in memory and
    the Authorization header. socket.timeout is caught explicitly because it
    only merges into TimeoutError in Python 3.10, and HTTPException because
    urlopen only wraps OSError into URLError.
    """
    request = urllib.request.Request(
        USAGE_URL,
        headers={"Authorization": "Bearer " + token, "anthropic-beta": OAUTH_BETA},
    )
    opener = urllib.request.build_opener(_BlockRedirects)

    try:
        with opener.open(request, timeout=HTTP_TIMEOUT) as response:
            return response.read(), None
    except urllib.error.HTTPError as error:
        return None, "usage request failed (HTTP {})".format(error.code)
    except socket.timeout:
        return None, "usage request timed out"
    except urllib.error.URLError as error:
        if isinstance(getattr(error, "reason", None), socket.timeout):
            return None, "usage request timed out"
        return None, "network error"
    except (OSError, http.client.HTTPException):
        return None, "network error"


def cache_file():
    """Returns the snapshot path, preferring SwiftBar's per-plugin cache dir."""
    base = os.environ.get("SWIFTBAR_PLUGIN_CACHE_PATH")
    if base:
        return os.path.join(base, "usage.json")
    return os.path.expanduser("~/.cache/claude-usage/usage.json")


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
    good snapshot. The temp file lives in the destination directory because
    os.replace is only atomic within one filesystem, and 0600 keeps the usage
    data private to the user.
    """
    try:
        if not is_usage_payload(json.loads(raw)):
            return
    except ValueError:
        return

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
    token, expires_at_ms, cred_reason = read_credentials()

    if token is not None and token_is_fresh(expires_at_ms, now):
        body, fetch_reason = fetch_usage(token)

        payload = None
        if body is not None:
            try:
                parsed = json.loads(body)
                payload = parsed if is_usage_payload(parsed) else None
            except ValueError:
                payload = None

        if payload is not None:
            write_cache(path, body)
            print("\n".join(render(payload, now, "fresh", None, now)))
            return

        reason = fetch_reason or "usage response unreadable"
        state = "stale-fetch"
    else:
        # Deliberately passive: refreshing the token here could rotate the
        # refresh token out from under Claude Code and log it out.
        reason = cred_reason or "token expired, open Claude Code to refresh"
        state = "stale-token"

    cached, fetched_at = read_cache(path)
    if cached is not None:
        print("\n".join(render(cached, now, state, reason, fetched_at)))
    else:
        print("\n".join(render(None, now, "error", reason, None)))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # A crash would blank the menu bar item entirely; an error glyph with
        # a generic reason is strictly more useful, so swallow everything.
        print("\n".join(render(None, datetime.now(timezone.utc), "error", "plugin error", None)))
        sys.stderr.write("claude-usage: unhandled error, see plugin output\n")
    sys.exit(0)
