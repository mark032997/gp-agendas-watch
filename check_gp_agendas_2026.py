import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone

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

def http_post_json(url: str, form: dict) -> dict:
    data = urllib.parse.urlencode(form).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Accept": "application/json, text/plain, */*",
            "User-Agent": "gp-agendas-watch/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    return json.loads(raw)

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
        # Some CivicPlus installs provide a direct file URL; if not, we still alert with name+id.
        file_url = d.get("FileUrl") or d.get("Url") or ""
        extracted.append({"id": doc_id, "name": name, "url": file_url})
    # stable sort for deterministic diffing
    extracted.sort(key=lambda x: (x["id"] if x["id"] is not None else 0, x["name"]))
    return extracted

def discord_notify(new_docs):
    if not DISCORD_WEBHOOK_URL:
        print("DISCORD_WEBHOOK_URL not set; skipping Discord notify.")
        return

    lines = []
    for doc in new_docs:
        if doc["url"]:
            lines.append(f"- **{doc['name']}**  ({doc['url']})")
        else:
            lines.append(f"- **{doc['name']}**  (ID: {doc['id']})")

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    content = "📄 **New document(s) in Galena Park → Agendas → 2026**\n" + "\n".join(lines) + f"\n\nChecked: {now}"

    data = json.dumps({"content": content}).encode("utf-8")
    req = urllib.request.Request(
        DISCORD_WEBHOOK_URL,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        resp.read()

def main():
    payload = http_post_json(URL, FORM)
    docs = extract_docs(payload)

    state = load_state()
    seen = set(state.get("seen_ids", []))

    current_ids = [d["id"] for d in docs if d["id"] is not None]
    current_set = set(current_ids)

    # First run: populate state, no alert (prevents spam)
    if not seen:
        state["seen_ids"] = sorted(list(current_set))
        save_state(state)
        print(f"Initialized state with {len(current_set)} document IDs.")
        return

    new_ids = current_set - seen
    if new_ids:
        new_docs = [d for d in docs if d["id"] in new_ids]
        discord_notify(new_docs)

        # update state
        state["seen_ids"] = sorted(list(current_set))
        save_state(state)
        print(f"Found {len(new_docs)} new docs; notified and updated state.")
    else:
        print("No new documents.")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
