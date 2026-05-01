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

CHAT_ID = "juritakagiap@icloud.com"


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
    active = {k: v for k, v in data["balances"].items() if abs(v) > 0.005}
    if not active:
        return "All settled up ✓"
    lines = ["Balances:"]
    for pid, amount in active.items():
        name = name_of(pid, data)
        if amount > 0:
            lines.append(f"  {name} is owed ${amount:.2f}")
        else:
            lines.append(f"  {name} owes ${abs(amount):.2f}")
    return "\n".join(lines)


def get_style_samples(limit=60):
    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        cur = conn.cursor()
        cur.execute("""
            SELECT m.text
            FROM message m
            JOIN chat_message_join cmj ON m.ROWID = cmj.message_id
            JOIN chat c ON cmj.chat_id = c.ROWID
            WHERE c.chat_identifier = ?
              AND m.text IS NOT NULL
              AND length(m.text) > 2
              AND length(m.text) < 180
            ORDER BY m.ROWID DESC
            LIMIT ?
        """, (CHAT_ID, limit))
        rows = cur.fetchall()
        conn.close()
        return [r[0] for r in rows if r[0]]
    except:
        return []


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


def build_style_context(samples):
    if not samples:
        return ""
    examples = "\n".join(f'  "{s}"' for s in samples[:25])
    return f"""
The people in this chat actually talk like this (real messages sampled):
{examples}

Match their tone exactly — casual, short, same energy, same kind of abbreviations or slang. Never be formal or robotic."""


def process_message(text, sender_id, data, style_context):
    people_info = {pid: name_of(pid, data) for pid in data["people"]}
    recent = data["transactions"][-5:] if data["transactions"] else []

    prompt = f"""You are Split, an expense tracking bot in a private chat.
{style_context}

Known people: {json.dumps(people_info)}
Current balances: {balances_text(data)}
Recent transactions: {json.dumps(recent)}

Message from {sender_id}: "{text}"

Return ONLY a single JSON object, no markdown, no extra text. Pick one action:

Expense (paid for something shared):
{{"action":"expense","payer_id":"SENDER","total":60.0,"description":"dinner","split_ids":["SENDER","other_id"],"reply":"casual short confirmation"}}

Debt (someone owes someone):
{{"action":"debt","owes_id":"SENDER_or_id","owed_id":"SENDER_or_id","amount":30.0,"description":"reason","reply":"casual short confirmation"}}

Settlement (paying someone back):
{{"action":"settle","from_id":"SENDER","to_id":"other_id","amount":20.0,"reply":"casual short confirmation"}}

Show balances: {{"action":"show_balances"}}
Show history: {{"action":"show_history"}}
Undo last entry: {{"action":"undo"}}
Register name (e.g. "I'm Alex"): {{"action":"register_name","name":"Alex"}}
Help: {{"action":"help"}}
Reset all: {{"action":"reset"}}
Unrelated: {{"action":"ignore"}}

Rules:
- "SENDER" = {sender_id}
- In a 2-person chat: "you owe me" → SENDER is owed; "I owe you" → SENDER owes
- split_ids should include everyone who shares the cost (including payer if they pay their own share)
- If no names given for a split, use all known members
- Keep reply short and match the chat's vibe
- Return ONLY valid JSON"""

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
        print(f"Parse error: {raw}")
        return {"action": "ignore"}


def resolve_id(id_str, sender_id):
    if not id_str:
        return sender_id
    return sender_id if id_str in ("SENDER", sender_id) else id_str


def apply_action(parsed, sender_id, data):
    action = parsed.get("action")

    def ensure(pid):
        data["balances"].setdefault(pid, 0.0)
        data["people"].setdefault(pid, pid[-7:] if len(pid) > 7 else pid)

    if action == "register_name":
        data["people"][sender_id] = parsed["name"]
        ensure(sender_id)
        return f"Got it, you're {parsed['name']} now"

    elif action == "help":
        return (
            "Split commands:\n"
            "• "I paid $X for Y split Z ways"\n"
            "• "You owe me $X for Y"\n"
            "• "I paid you back $X"\n"
            "• "show balances"\n"
            "• "show history"\n"
            "• "undo last"\n"
            "• "reset balances"\n"
            "• "I'm [name]" to set your name"
        )

    elif action == "show_history":
        if not data["transactions"]:
            return "No transactions yet"
        lines = ["Last transactions:"]
        for t in data["transactions"][-10:]:
            a = t.get("action", "")
            if a == "expense":
                lines.append(f"  ${t.get('total', 0):.2f} for {t.get('description', '?')}")
            elif a == "debt":
                lines.append(f"  ${t.get('amount', 0):.2f} debt — {t.get('description', '?')}")
            elif a == "settle":
                lines.append(f"  ${t.get('amount', 0):.2f} settled")
        return "\n".join(lines)

    elif action == "undo":
        if not data["transactions"]:
            return "Nothing to undo"
        last = data["transactions"].pop()
        a = last.get("action", "")
        if a == "expense":
            payer = last.get("resolved_payer", sender_id)
            split_ids = [resolve_id(i, sender_id) for i in last.get("split_ids", [])]
            total = float(last.get("total", 0))
            if split_ids:
                share = total / len(split_ids)
                for pid in split_ids:
                    if pid != payer:
                        data["balances"][pid] = data["balances"].get(pid, 0) + share
                        data["balances"][payer] = data["balances"].get(payer, 0) - share
        elif a == "debt":
            owes = resolve_id(last.get("owes_id", ""), sender_id)
            owed = resolve_id(last.get("owed_id", ""), sender_id)
            amount = float(last.get("amount", 0))
            if owes and owed:
                data["balances"][owes] = data["balances"].get(owes, 0) + amount
                data["balances"][owed] = data["balances"].get(owed, 0) - amount
        elif a == "settle":
            from_id = resolve_id(last.get("from_id", ""), sender_id)
            to_id = resolve_id(last.get("to_id", ""), sender_id)
            amount = float(last.get("amount", 0))
            if from_id and to_id:
                data["balances"][from_id] = data["balances"].get(from_id, 0) - amount
                data["balances"][to_id] = data["balances"].get(to_id, 0) + amount
        return f"Undone.\n{balances_text(data)}"

    elif action == "reset":
        data["balances"] = {pid: 0.0 for pid in data["balances"]}
        data["transactions"] = []
        return "Cleared everything."

    elif action == "expense":
        payer = resolve_id(parsed.get("payer_id", "SENDER"), sender_id)
        total = float(parsed["total"])
        split_ids = [resolve_id(i, sender_id) for i in parsed.get("split_ids", [])]

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
        reply = parsed.get("reply", f"Added ${total:.2f} for {parsed.get('description', 'expense')}.")
        return f"{reply}\n{balances_text(data)}"

    elif action == "debt":
        owes = resolve_id(parsed.get("owes_id", "SENDER"), sender_id)
        owed = resolve_id(parsed.get("owed_id", "SENDER"), sender_id)
        amount = float(parsed["amount"])
        for pid in [owes, owed]:
            ensure(pid)
        data["balances"][owes] -= amount
        data["balances"][owed] += amount
        data["transactions"].append(parsed)
        reply = parsed.get("reply", f"Recorded ${amount:.2f}.")
        return f"{reply}\n{balances_text(data)}"

    elif action == "settle":
        from_id = resolve_id(parsed.get("from_id", "SENDER"), sender_id)
        to_id = resolve_id(parsed.get("to_id", ""), sender_id)
        amount = float(parsed["amount"])
        for pid in [from_id, to_id]:
            ensure(pid)
        data["balances"][from_id] += amount
        data["balances"][to_id] -= amount
        data["transactions"].append(parsed)
        reply = parsed.get("reply", f"Settled ${amount:.2f}.")
        return f"{reply}\n{balances_text(data)}"

    elif action == "show_balances":
        return balances_text(data)

    return None


# ── Start ─────────────────────────────────────────────────────────────────────
data = load_data()

conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
last_seen_rowid = conn.execute("SELECT MAX(ROWID) FROM message").fetchone()[0] or 0
conn.close()

style_samples = get_style_samples()
style_context = build_style_context(style_samples)

print(f"Split running. Watching: {CHAT_ID}")
print(f"Style samples loaded: {len(style_samples)}")
print("Ctrl+C to stop.\n")

while True:
    for row in get_new_messages():
        last_seen_rowid = row["ROWID"]
        sender = row["sender"]
        text = row["text"]
        print(f"[{sender[-12:]}]: {text}")

        data["people"].setdefault(sender, sender[-7:] if len(sender) > 7 else sender)
        data["balances"].setdefault(sender, 0.0)

        parsed = process_message(text, sender, data, style_context)
        print(f"  → {parsed.get('action')}")

        reply = apply_action(parsed, sender, data)
        if reply:
            save_data(data)
            send_reply(reply)
            print(f"  → sent: {reply[:100]}")

    time.sleep(3)
