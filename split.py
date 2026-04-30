import sqlite3
import subprocess
import time
import os
import json
import re
from anthropic import Anthropic

DB_PATH = os.path.expanduser("~/Library/Messages/chat.db")
DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.json")
client = Anthropic()

# ── Set this to your group chat's identifier (run list_chats.py first) ──────
CHAT_ID = "juritakagiap@icloud.com"
# ─────────────────────────────────────────────────────────────────────────────


def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE) as f:
            return json.load(f)
    return {"people": {}, "balances": {}, "transactions": []}


def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)


def name_of(pid, data):
    return data["people"].get(pid, pid[-7:] if len(pid) > 7 else pid)


def balances_text(data):
    if not any(abs(v) > 0.005 for v in data["balances"].values()):
        return "Everyone is settled up."
    lines = ["💰 Balances:"]
    for pid, amount in data["balances"].items():
        name = name_of(pid, data)
        if amount > 0.005:
            lines.append(f"  {name} is owed ${amount:.2f}")
        elif amount < -0.005:
            lines.append(f"  {name} owes ${abs(amount):.2f}")
        else:
            lines.append(f"  {name} ✓")
    return "\n".join(lines)


last_seen_rowid = 0


def get_new_messages():
    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("""
            SELECT m.ROWID, m.text, COALESCE(h.id, 'me') as sender
            FROM message m
            JOIN chat_message_join cmj ON m.ROWID = cmj.message_id
            JOIN chat c ON cmj.chat_id = c.ROWID
            LEFT JOIN handle h ON m.handle_id = h.ROWID
            WHERE m.is_from_me = 0
              AND m.ROWID > ?
              AND m.text IS NOT NULL
              AND c.chat_identifier = ?
            ORDER BY m.ROWID ASC
        """, (last_seen_rowid, CHAT_ID))
        rows = cur.fetchall()
        conn.close()
        return rows
    except Exception as e:
        print(f"DB error: {e}")
        return []


def send_reply(text):
    safe = text.replace("\\", "\\\\").replace('"', '\\"')
    script = f'''
    tell application "Messages"
        set targetService to 1st service whose service type = iMessage
        set targetBuddy to buddy "{CHAT_ID}" of targetService
        send "{safe}" to targetBuddy
    end tell
    '''
    result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    if result.returncode != 0:
        print(f"Send error: {result.stderr}")


def process_message(text, sender_id, data):
    people_info = {pid: name_of(pid, data) for pid in data["people"]}

    prompt = f"""You are a group chat expense-tracking bot like Splitwise.

Current members and their IDs:
{json.dumps(people_info, indent=2)}

Current balances:
{balances_text(data)}

Message sent by: {sender_id}
Message: "{text}"

Return ONLY a JSON object (no markdown, no extra text). Choose one:

Expense (e.g. "I paid $60 for dinner split 3 ways"):
{{"action":"expense","payer_id":"SENDER","total":60.0,"description":"dinner","split_ids":["SENDER","id2","id3"],"reply":"short confirmation"}}

Direct debt (e.g. "John owes me $30"):
{{"action":"debt","owes_id":"john_id","owed_id":"SENDER","amount":30.0,"description":"reason","reply":"short confirmation"}}

Settlement (e.g. "I paid John back $20"):
{{"action":"settle","from_id":"SENDER","to_id":"john_id","amount":20.0,"reply":"short confirmation"}}

Show balances (e.g. "show balances", "who owes what"):
{{"action":"show_balances"}}

Name registration (e.g. "I'm Alex", "call me Sarah"):
{{"action":"register_name","name":"Alex"}}

Unrelated message:
{{"action":"ignore"}}

Use "SENDER" to refer to the person who sent this message.
For split_ids, include everyone splitting (including the payer if they share the cost).
If the message says "split N ways" but doesn't name people, use all known members."""

    resp = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=400,
        messages=[{"role": "user", "content": prompt}]
    )

    raw = resp.content[0].text.strip()
    raw = re.sub(r'^```(?:json)?\n?', '', raw)
    raw = re.sub(r'\n?```$', '', raw)

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        print(f"Could not parse Claude response: {raw}")
        return {"action": "ignore"}


def resolve_id(id_str, sender_id):
    return sender_id if id_str in ("SENDER", sender_id) else id_str


def apply_action(parsed, sender_id, data):
    action = parsed.get("action")

    def ensure(pid):
        data["balances"].setdefault(pid, 0.0)
        data["people"].setdefault(pid, pid[-7:] if len(pid) > 7 else pid)

    if action == "register_name":
        data["people"][sender_id] = parsed["name"]
        ensure(sender_id)
        return f"Got it, I'll call you {parsed['name']}!"

    elif action == "expense":
        payer = resolve_id(parsed.get("payer_id", "SENDER"), sender_id)
        total = float(parsed["total"])
        raw_splits = parsed.get("split_ids", [])
        split_ids = [resolve_id(i, sender_id) for i in raw_splits]

        if not split_ids:
            split_ids = list(data["balances"].keys()) or [sender_id]

        for pid in [payer] + split_ids:
            ensure(pid)

        share = total / len(split_ids)
        for pid in split_ids:
            if pid != payer:
                data["balances"][pid] -= share
                data["balances"][payer] += share

        data["transactions"].append({**parsed, "resolved_payer": payer})
        return parsed.get("reply", f"Added ${total:.2f} for {parsed.get('description','expense')}.")

    elif action == "debt":
        owes = resolve_id(parsed.get("owes_id", "SENDER"), sender_id)
        owed = resolve_id(parsed.get("owed_id", "SENDER"), sender_id)
        amount = float(parsed["amount"])
        for pid in [owes, owed]:
            ensure(pid)
        data["balances"][owes] -= amount
        data["balances"][owed] += amount
        data["transactions"].append(parsed)
        return parsed.get("reply", f"Recorded ${amount:.2f} debt.")

    elif action == "settle":
        from_id = resolve_id(parsed.get("from_id", "SENDER"), sender_id)
        to_id = resolve_id(parsed.get("to_id", ""), sender_id)
        amount = float(parsed["amount"])
        for pid in [from_id, to_id]:
            ensure(pid)
        data["balances"][from_id] += amount
        data["balances"][to_id] -= amount
        data["transactions"].append(parsed)
        return parsed.get("reply", f"Settled ${amount:.2f}.")

    elif action == "show_balances":
        return balances_text(data)

    return None


# ── Start ────────────────────────────────────────────────────────────────────
if CHAT_ID == "REPLACE_ME":
    print("ERROR: Open bot.py and set CHAT_ID first.")
    print("Run list_chats.py to find your group chat's identifier.")
    exit(1)

data = load_data()

conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
last_seen_rowid = conn.execute("SELECT MAX(ROWID) FROM message").fetchone()[0] or 0
conn.close()

print(f"Splitwise bot running. Watching: {CHAT_ID}")
print("Ctrl+C to stop.\n")

while True:
    for row in get_new_messages():
        last_seen_rowid = row["ROWID"]
        sender = row["sender"]
        text = row["text"]
        print(f"[{sender[-10:]}]: {text}")

        data["people"].setdefault(sender, sender[-7:] if len(sender) > 7 else sender)
        data["balances"].setdefault(sender, 0.0)

        parsed = process_message(text, sender, data)
        print(f"  → {parsed.get('action')}")

        reply = apply_action(parsed, sender, data)
        if reply:
            save_data(data)
            send_reply(reply)
            print(f"  → sent: {reply[:80]}")

    time.sleep(3)
