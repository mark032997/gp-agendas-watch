import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta

# --- CivicPlus endpoint + folder payload (Agendas -> 2026) ---
URL = "https://www.cityofgalenapark-tx.gov/Admin/DocumentCenter/Home/Document_AjaxBinding?renderMode=0&loadSource=7"

FORM = {
    "folderId": "132",
    "getDocuments": "1",
    "imageRepo": "false",
    "renderMode": "0",
    "loadSource": "7",
    "pageNumber": "1",
    "requestingModuleID": "75",
    "rowsPerPage": "100",
    "searchString": "",
    "sortColumn": "DisplayName",
    "sortOrder": "0",
}

STATE_FILE = "state.json"
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()

# Heartbeat only on manual runs (workflow sets this)
FORCE_NOTIFY = os.environ.get("FORCE_NOTIFY", "").strip() in ("1", "true", "TRUE", "yes", "YES")

# Daily 7pm CST message only on the "daily schedule" runs (workflow sets this)
DAILY_CHECK = os.environ.get("DAILY_CHECK", "").strip() in ("1", "true", "TRUE", "yes", "YES")

# America/Chicago handling without external deps:
# We'll compute local time using system's UTC + an offset, plus a simple DST rule.
# This is good enough for "7pm in Chicago" for automation purposes.
def is_us_dst_chicago(dt_utc: datetime) -> bool:
    """
    Approx DST for America/Chicago:
    - starts: 2nd Sunday in March at 2:00 local
    - ends:   1st Sunday in November at 2:00 local
    We evaluate boundaries in UTC approximately by converting candidate local times.
    """
    year = dt_utc.year

    # Find 2nd Sunday in March
    march = datetime(year, 3, 1, tzinfo=timezone.utc)
    # weekday(): Mon=0...Sun=6; need Sunday=6
    first_sunday_offset = (6 - march.weekday()) % 7
    second_sunday = 1 + first_sunday_offset + 7
    # 2:00 local CST (UTC-6) before switch => 08:00 UTC
    dst_start_utc = datetime(year, 3, second_sunday, 8, 0, 0, tzinfo=timezone.utc)

    # Find 1st Sunday in November
    nov = datetime(year, 11, 1, tzinfo=timezone.utc)
    first_sunday_offset = (6 - nov.weekday()) % 7
    first_sunday = 1 + first_sunday_offset
    # 2:00 local CDT (UTC-5) before switch => 07:00 UTC
    dst_end_utc = datetime(year, 11, first_sunday, 7, 0, 0, tzinfo=timezone.utc)

    return dst_start_utc <= dt_utc < dst_end_utc

def chicago_now_from_utc(dt_utc: datetime) -> datetime:
    # CST = UTC-6, CDT = UTC-5
    offset_hours = -5 if is_us_dst_chicago(dt_utc) else -6
    return (dt_utc + timedelta(hours=offset_hours)).replace(tzinfo=None)

def http_post_json(url: str, form: dict) -> dict:
    """
    CivicPlus sometimes blocks datacenter IPs. Use browser-ish headers + retries.
    """
    data = urllib.parse.urlencode(form).encode("utf-8")

    headers = {
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Accept": "application/json, text/plain, */*",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "X-Requested-With": "XMLHttpRequest",
        "Origin": "https://www.cityofgalenapark-tx.gov",
        "Referer": "https://www.cityofgalenapark-tx.gov/DocumentCenter",
    }

    last_err = None
    for attempt in range(1, 6):
        try:
            req = urllib.request.Request(url, data=data, method="POST", headers=headers)
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw)
        except Exception as e:
            last_err = e
            time.sleep(2 ** (attempt - 1))  # 1,2,4,8,16 sec
    raise last_err

def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"seen_ids": []}

def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def extract_docs(payload: dict):
    docs = payload.get("Documents", []) or []
    extracted = []
    for d in docs:
        doc_id = d.get("ID")
        name = d.get("DisplayName") or d.get("Name") or "(unnamed)"
        # Some installs include direct URLs, many don't. We'll alert with ID regardless.
        url = d.get("FileUrl") or d.get("Url") or ""
        extracted.append({"id": doc_id, "name": name, "url": url})
    extracted.sort(key=lambda x: (x["id"] if x["id"] is not None else 0, x["name"]))
    return extracted

def discord_post(text: str):
    if not DISCORD_WEBHOOK_URL:
        print("DISCORD_WEBHOOK_URL not set; skipping Discord notify.")
        return
    data = json.dumps({"content": text}).encode("utf-8")
    req = urllib.request.Request(
        DISCORD_WEBHOOK_URL,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        resp.read()

def main():
    dt_utc = datetime.now(timezone.utc)
    dt_local = chicago_now_from_utc(dt_utc)
    now_utc_str = dt_utc.strftime("%Y-%m-%d %H:%M UTC")
    now_local_str = dt_local.strftime("%Y-%m-%d %I:%M %p America/Chicago")

    payload = http_post_json(URL, FORM)
    docs = extract_docs(payload)

    current_ids = [d["id"] for d in docs if d["id"] is not None]
    current_set = set(current_ids)

    state = load_state()
    seen = set(state.get("seen_ids", []))

    # First run: initialize state (no "new doc" ping), but allow heartbeat if manual.
    if not seen:
        state["seen_ids"] = sorted(list(current_set))
        save_state(state)

        msg = (
            f"✅ GP Agendas 2026 watcher initialized.\n"
            f"Total docs: {len(current_set)}\n"
            f"Checked: {now_utc_str} | {now_local_str}"
        )
        print(msg)
        if FORCE_NOTIFY:
            discord_post(msg)
        return

    new_ids = current_set - seen

    if new_ids:
        new_docs = [d for d in docs if d["id"] in new_ids]
        lines = []
        for doc in new_docs:
            if doc["url"]:
                lines.append(f"- **{doc['name']}** ({doc['url']})")
            else:
                lines.append(f"- **{doc['name']}** (ID: {doc['id']})")

        msg = (
            "📄 **New document(s) in Galena Park → Agendas → 2026**\n"
            + "\n".join(lines)
            + f"\n\nChecked: {now_utc_str} | {now_local_str}"
        )
        discord_post(msg)
        print(f"Found {len(new_docs)} new docs; notified.")

        # Update state after successful notification
        state["seen_ids"] = sorted(list(current_set))
        save_state(state)
        return

    # No new documents
    print(f"No new documents. Total docs: {len(current_set)}. Checked: {now_utc_str} | {now_local_str}")

    # Heartbeat on manual runs
    if FORCE_NOTIFY:
        discord_post(
            f"✅ GP Agendas 2026 check OK — no changes.\n"
            f"Total docs: {len(current_set)}\n"
            f"Checked: {now_utc_str} | {now_local_str}"
        )

    # Daily 7pm America/Chicago message (only on the daily scheduled runs)
    # If workflow runs at the wrong UTC time (DST), we still only send if local hour==19.
    if DAILY_CHECK and dt_local.hour == 19:
        discord_post(
            f"No new documents my leige.\n"
            f"Total docs: {len(current_set)}\n"
            f"Checked: {now_utc_str} | {now_local_str}"
        )

    # Always update state (even if unchanged) so it remains consistent
    state["seen_ids"] = sorted(list(current_set))
    save_state(state)

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        dt_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        err = f"❌ GP Agendas 2026 watcher ERROR: {e}\nChecked: {dt_utc}"
        print(err, file=sys.stderr)
        try:
            if DISCORD_WEBHOOK_URL:
                discord_post(err)
        except Exception:
            pass
        sys.exit(1)
