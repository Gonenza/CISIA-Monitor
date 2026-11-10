#!/usr/bin/env python3
"""
CISIA CEnT-S Calendar Monitor
==============================
Checks https://testcisia.it/calendario.php?tolc=cents&l=gb&lingua=inglese
for real changes to the test-dates table (new rows, seats appearing/
disappearing, or STATE flipping between "AVAILABLE SEATS" and
"NOT LONGER AVAILABLE"), and sends a Telegram alert the instant it
detects one.

Designed to run unattended inside a GitHub Actions workflow every
5 minutes. It is stateless between runs except for a single JSON file
(state.json) that this script reads and rewrites, and that the
workflow commits back to the repo so the next run can see it.

Exit codes:
  0 -> ran fine (whether or not a change was found)
  1 -> fetch/parse failed (workflow step will show as failed in the
       Actions tab, which is your "is it still alive" signal)
"""

import io
import json
import os
import sys
from datetime import datetime, timezone

import requests
import pandas as pd

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

TARGET_URL = "https://testcisia.it/calendario.php?tolc=cents&l=gb&lingua=inglese"
STATE_FILE = "state.json"

# Columns we expect in the table, in the order CISIA currently uses.
# read_html gives us these as the DataFrame's column headers.
EXPECTED_COLUMNS = {
    "FORMAT", "UNIVERSITY", "REGION / FOREIGN COUNTRY", "CITY",
    "BOOKINGS DEADLINE", "SEATS", "STATE", "DATE",
}

# A row is uniquely identified by these fields (the things that define
# "which test slot this is"). SEATS and STATE are the fields we watch
# for changes WITHIN a given slot.
KEY_COLUMNS = ["FORMAT", "UNIVERSITY", "REGION / FOREIGN COUNTRY", "CITY",
               "BOOKINGS DEADLINE", "DATE"]
WATCH_COLUMNS = ["SEATS", "STATE"]

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

HEADERS = {
    # A normal browser User-Agent avoids being blocked as a bot.
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def send_telegram(text: str) -> None:
    """Send a message via the Telegram Bot API. Silently logs failures
    instead of crashing the whole run — a failed notification shouldn't
    stop us from saving the new state."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("WARNING: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set; "
              "skipping notification. Message was:\n" + text)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(
            url,
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "disable_web_page_preview": False,
            },
            timeout=20,
        )
        resp.raise_for_status()
    except Exception as e:
        print(f"ERROR: Telegram notification failed: {e}")


# ---------------------------------------------------------------------------
# Fetch + parse
# ---------------------------------------------------------------------------

def fetch_table() -> pd.DataFrame:
    """Download the page and return the test-dates table as a DataFrame."""
    resp = requests.get(TARGET_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()

    tables = pd.read_html(io.StringIO(resp.text))
    if not tables:
        raise ValueError("No <table> elements found on the page at all.")

    # Pick the table whose columns best match what we expect. CISIA's page
    # currently has exactly one data table, but this makes us resilient if
    # they ever add another one elsewhere on the page.
    best = None
    best_score = -1
    for t in tables:
        cols = {str(c).strip().upper() for c in t.columns}
        score = len(cols & EXPECTED_COLUMNS)
        if score > best_score:
            best_score = score
            best = t

    if best is None or best_score < 4:
        raise ValueError(
            "Could not find the test-dates table on the page — CISIA may "
            "have changed the page layout. Manual check needed."
        )

    best.columns = [str(c).strip().upper() for c in best.columns]
    # Normalize all cell values to clean strings.
    for col in best.columns:
        best[col] = best[col].astype(str).str.strip()
    return best


def df_to_rows(df: pd.DataFrame) -> dict:
    """Convert the DataFrame into {row_key: {seats, state}} so we can diff
    it against the previous snapshot."""
    rows = {}
    for _, r in df.iterrows():
        key_parts = [str(r.get(c, "")) for c in KEY_COLUMNS]
        key = " | ".join(key_parts)
        rows[key] = {col: str(r.get(col, "")) for col in WATCH_COLUMNS}
    return rows


# ---------------------------------------------------------------------------
# State load/save
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {"rows": {}, "consecutive_failures": 0, "last_ok": None}
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False, sort_keys=True)


# ---------------------------------------------------------------------------
# Diffing
# ---------------------------------------------------------------------------

def diff_rows(old_rows: dict, new_rows: dict) -> list:
    """Return a list of human-readable change descriptions."""
    changes = []

    old_keys = set(old_rows.keys())
    new_keys = set(new_rows.keys())

    for key in sorted(new_keys - old_keys):
        state = new_rows[key]["STATE"]
        seats = new_rows[key]["SEATS"]
        changes.append(f"🆕 NEW SLOT ADDED:\n{key}\n→ Seats: {seats} | State: {state}")

    for key in sorted(old_keys - new_keys):
        changes.append(f"❌ SLOT REMOVED FROM CALENDAR:\n{key}")

    for key in sorted(old_keys & new_keys):
        old_v = old_rows[key]
        new_v = new_rows[key]
        if old_v != new_v:
            parts = []
            if old_v.get("STATE") != new_v.get("STATE"):
                parts.append(f'STATE: "{old_v.get("STATE")}" → "{new_v.get("STATE")}"')
            if old_v.get("SEATS") != new_v.get("SEATS"):
                parts.append(f'SEATS: "{old_v.get("SEATS")}" → "{new_v.get("SEATS")}"')
            if parts:
                flag = "🟢" if "AVAILABLE SEATS" in new_v.get("STATE", "") and \
                               "AVAILABLE SEATS" not in old_v.get("STATE", "") else "🔁"
                changes.append(f"{flag} CHANGED:\n{key}\n→ " + " | ".join(parts))

    return changes


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    state = load_state()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    try:
        df = fetch_table()
        new_rows = df_to_rows(df)
    except Exception as e:
        state["consecutive_failures"] = state.get("consecutive_failures", 0) + 1
        print(f"ERROR fetching/parsing page: {e}")
        # Only alert after 3 consecutive failures (~15 min of real downtime)
        # so a single network blip doesn't spam you.
        if state["consecutive_failures"] == 3:
            send_telegram(
                "⚠️ CEnT-S monitor: the calendar page has failed to load or "
                f"parse for 3 checks in a row as of {now}.\n"
                f"Error: {e}\n{TARGET_URL}"
            )
        save_state(state)
        return 1

    is_first_run = not state.get("rows")
    old_rows = state.get("rows", {})

    if is_first_run:
        print(f"First run: baseline saved with {len(new_rows)} rows.")
        send_telegram(
            "✅ CEnT-S monitor is now live.\n"
            f"Tracking {len(new_rows)} test slots.\n"
            f"I'll alert you the instant something changes.\n{TARGET_URL}"
        )
    else:
        changes = diff_rows(old_rows, new_rows)
        if changes:
            message = (
                "🔔 CEnT-S calendar changed!\n"
                f"({now})\n\n" + "\n\n".join(changes) +
                f"\n\n👉 Book now: {TARGET_URL}"
            )
            print(message)
            send_telegram(message)
        else:
            print(f"{now}: checked, no changes.")

    state["rows"] = new_rows
    state["consecutive_failures"] = 0
    state["last_ok"] = now
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
