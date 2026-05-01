import os
import json
import re
from flask import Flask, request
from twilio.rest import Client
from twilio.twiml.messaging_response import MessagingResponse
from anthropic import Anthropic

app = Flask(__name__)
anthropic = Anthropic()

print("ENV KEYS:", list(os.environ.keys()))

TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_NUMBER = os.environ.get("TWILIO_NUMBER", "+19784048707")

print("SID present:", bool(TWILIO_ACCOUNT_SID))
print("TOKEN present:", bool(TWILIO_AUTH_TOKEN))

twilio = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN) if TWILIO_ACCOUNT_SID else None

DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.json")

FREE_INTERACTIONS = 20  # interactions before paywall (future use)


# ── Data ──────────────────────────────────────────────────────────────────────

def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE) as f:
            return json.load(f)
    return {}


def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)


def get_user(data, phone):
    if phone not in data:
        data[phone] = {
            "name": None,
            "group": None,
            "interactions": 0,
            "opted_in": False,
        }
    return data[phone]


def get_group(data, group_id):
    if "groups" not in data:
        data["groups"] = {}
    if group_id not in data["groups"]:
        data["groups"][group_id] = {
            "members": [],
            "balances": {},
            "transactions": [],
        }
    return data["groups"][group_id]


# ── Helpers ───────────────────────────────────────────────────────────────────

def name_of(phone, data):
    user = data.get(phone, {})
    n = user.get("name")
    if n:
        return n
    return phone[-4:]


def balances_text(group, data):
    active = {k: v for k, v in group["balances"].items() if abs(v) > 0.005}
    if not active:
        return "All settled up ✓"
    lines = ["Balances:"]
    for phone, amount in active.items():
        n = name_of(phone, data)
        if amount > 0:
            lines.append(f"  {n} is owed ${amount:.2f}")
        else:
            lines.append(f"  {n} owes ${abs(amount):.2f}")
    return "\n".join(lines)


def resolve_id(id_str, sender, group):
    if id_str in ("SENDER", sender):
        return sender
    # try to match by name
    for phone in group["members"]:
        if id_str.lower() in phone.lower():
            return phone
    return id_str


# ── AI ────────────────────────────────────────────────────────────────────────

def process_message(text, sender, group, data):
    members_info = {p: name_of(p, data) for p in group["members"]}
    recent = group["transactions"][-5:]

    prompt = f"""You are Split, an SMS expense tracking bot.

Group members: {json.dumps(members_info)}
Balances: {balances_text(group, data)}
Recent transactions: {json.dumps(recent)}

Message from {sender} ({name_of(sender, data)}): "{text}"

Return ONLY a single JSON object, no markdown.

Actions:

Expense: {{"action":"expense","payer_id":"SENDER","total":60.0,"description":"dinner","split_ids":["SENDER","other_phone"],"reply":"short confirmation"}}
Debt: {{"action":"debt","owes_id":"SENDER_or_phone","owed_id":"SENDER_or_phone","amount":30.0,"description":"reason","reply":"short confirmation"}}
Settle: {{"action":"settle","from_id":"SENDER","to_id":"other_phone","amount":20.0,"reply":"short confirmation"}}
Show balances: {{"action":"show_balances"}}
Show history: {{"action":"show_history"}}
Undo: {{"action":"undo"}}
Set name: {{"action":"register_name","name":"Alex"}}
Add someone (e.g. "add +15551234567"): {{"action":"add_member","phone":"+15551234567"}}
Help: {{"action":"help"}}
Reset: {{"action":"reset"}}
Unrelated: {{"action":"ignore"}}

Rules:
- "SENDER" = {sender}
- "you owe me" → SENDER is owed, other person owes
- "I owe you" → SENDER owes, other person is owed
- split_ids = everyone sharing cost including payer
- If no names given for split, split among all members
- Keep reply short and casual"""

    resp = anthropic.messages.create(
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


# ── Actions ───────────────────────────────────────────────────────────────────

def apply_action(parsed, sender, group, data):
    action = parsed.get("action")

    def ensure(phone):
        group["balances"].setdefault(phone, 0.0)
        if phone not in group["members"]:
            group["members"].append(phone)

    if action == "register_name":
        data[sender]["name"] = parsed["name"]
        ensure(sender)
        return f"Got it, you're {parsed['name']} now."

    elif action == "add_member":
        phone = parsed.get("phone", "").strip()
        if phone and phone not in group["members"]:
            ensure(phone)
            return f"Added {phone} to the group. Have them text this number to get started."
        return "They're already in the group."

    elif action == "help":
        return (
            "Split commands:\n"
            "• I paid $X for Y split Z ways\n"
            "• You owe me $X for Y\n"
            "• I paid you back $X\n"
            "• add +1XXXXXXXXXX\n"
            "• show balances\n"
            "• show history\n"
            "• undo last\n"
            "• reset balances\n"
            "• I'm [name]"
        )

    elif action == "show_history":
        if not group["transactions"]:
            return "No transactions yet."
        lines = ["Last transactions:"]
        for t in group["transactions"][-10:]:
            a = t.get("action", "")
            if a == "expense":
                lines.append(f"  ${t.get('total', 0):.2f} for {t.get('description', '?')}")
            elif a == "debt":
                lines.append(f"  ${t.get('amount', 0):.2f} — {t.get('description', '?')}")
            elif a == "settle":
                lines.append(f"  ${t.get('amount', 0):.2f} settled")
        return "\n".join(lines)

    elif action == "undo":
        if not group["transactions"]:
            return "Nothing to undo."
        last = group["transactions"].pop()
        a = last.get("action", "")
        if a == "expense":
            payer = last.get("resolved_payer", sender)
            split_ids = last.get("resolved_splits", [])
            total = float(last.get("total", 0))
            if split_ids:
                share = total / len(split_ids)
                for pid in split_ids:
                    if pid != payer:
                        group["balances"][pid] = group["balances"].get(pid, 0) + share
                        group["balances"][payer] = group["balances"].get(payer, 0) - share
        elif a == "debt":
            owes = last.get("resolved_owes")
            owed = last.get("resolved_owed")
            amount = float(last.get("amount", 0))
            if owes and owed:
                group["balances"][owes] = group["balances"].get(owes, 0) + amount
                group["balances"][owed] = group["balances"].get(owed, 0) - amount
        elif a == "settle":
            from_id = last.get("resolved_from")
            to_id = last.get("resolved_to")
            amount = float(last.get("amount", 0))
            if from_id and to_id:
                group["balances"][from_id] = group["balances"].get(from_id, 0) - amount
                group["balances"][to_id] = group["balances"].get(to_id, 0) + amount
        return f"Undone.\n{balances_text(group, data)}"

    elif action == "reset":
        group["balances"] = {p: 0.0 for p in group["members"]}
        group["transactions"] = []
        return "Cleared everything."

    elif action == "expense":
        payer = resolve_id(parsed.get("payer_id", "SENDER"), sender, group)
        total = float(parsed["total"])
        split_ids = [resolve_id(i, sender, group) for i in parsed.get("split_ids", [])]

        if not split_ids:
            split_ids = group["members"] or [sender]

        for pid in [payer] + split_ids:
            ensure(pid)

        share = total / len(split_ids)
        for pid in split_ids:
            if pid != payer:
                group["balances"][pid] -= share
                group["balances"][payer] += share

        group["transactions"].append({
            **parsed,
            "resolved_payer": payer,
            "resolved_splits": split_ids,
        })
        reply = parsed.get("reply", f"Added ${total:.2f} for {parsed.get('description', 'expense')}.")
        return f"{reply}\n{balances_text(group, data)}"

    elif action == "debt":
        owes = resolve_id(parsed.get("owes_id", "SENDER"), sender, group)
        owed = resolve_id(parsed.get("owed_id", "SENDER"), sender, group)
        amount = float(parsed["amount"])
        for pid in [owes, owed]:
            ensure(pid)
        group["balances"][owes] -= amount
        group["balances"][owed] += amount
        group["transactions"].append({
            **parsed,
            "resolved_owes": owes,
            "resolved_owed": owed,
        })
        reply = parsed.get("reply", f"Recorded ${amount:.2f}.")
        return f"{reply}\n{balances_text(group, data)}"

    elif action == "settle":
        from_id = resolve_id(parsed.get("from_id", "SENDER"), sender, group)
        to_id = resolve_id(parsed.get("to_id", ""), sender, group)
        amount = float(parsed["amount"])
        for pid in [from_id, to_id]:
            ensure(pid)
        group["balances"][from_id] += amount
        group["balances"][to_id] -= amount
        group["transactions"].append({
            **parsed,
            "resolved_from": from_id,
            "resolved_to": to_id,
        })
        reply = parsed.get("reply", f"Settled ${amount:.2f}.")
        return f"{reply}\n{balances_text(group, data)}"

    elif action == "show_balances":
        return balances_text(group, data)

    return None


# ── Webhook ───────────────────────────────────────────────────────────────────

@app.route("/sms", methods=["POST"])
def sms_webhook():
    sender = request.form.get("From", "")
    text = request.form.get("Body", "").strip()
    resp = MessagingResponse()

    print(f"[{sender}]: {text}")

    # Handle opt-out (Twilio handles STOP automatically, but track it)
    if text.upper() in ("STOP", "QUIT", "CANCEL", "END", "UNSUBSCRIBE"):
        data = load_data()
        user = get_user(data, sender)
        user["opted_in"] = False
        save_data(data)
        return str(resp)  # Twilio sends the opt-out message automatically

    # Handle opt-in
    if text.upper() == "START":
        data = load_data()
        user = get_user(data, sender)
        user["opted_in"] = True
        # Auto-add to a default group keyed by their number
        group_id = sender
        group = get_group(data, group_id)
        user["group"] = group_id
        if sender not in group["members"]:
            group["members"].append(sender)
            group["balances"][sender] = 0.0
        save_data(data)
        resp.message(
            "Welcome to Split! 💸\n"
            "Track expenses with friends over text.\n\n"
            "Text 'I'm [name]' to set your name.\n"
            "Text 'help' for all commands."
        )
        return str(resp)

    data = load_data()
    user = get_user(data, sender)

    if not user.get("opted_in"):
        resp.message("Text START to use Split.")
        return str(resp)

    # Get or create group
    group_id = user.get("group", sender)
    group = get_group(data, group_id)

    if sender not in group["members"]:
        group["members"].append(sender)
        group["balances"].setdefault(sender, 0.0)

    user["interactions"] = user.get("interactions", 0) + 1

    parsed = process_message(text, sender, group, data)
    print(f"  → {parsed.get('action')}")

    reply = apply_action(parsed, sender, group, data)

    if reply:
        save_data(data)
        resp.message(reply)
        print(f"  → sent: {reply[:100]}")

    return str(resp)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(port=port, debug=False)
