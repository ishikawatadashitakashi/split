import sqlite3, os

DB = os.path.expanduser("~/Library/Messages/chat.db")
conn = sqlite3.connect(f"file:{DB}?mode=ro&immutable=1", uri=True)

rows = conn.execute("""
    SELECT c.chat_identifier, c.display_name,
           COUNT(cmj.message_id) as msg_count,
           MAX(m.date) as last_date
    FROM chat c
    JOIN chat_message_join cmj ON c.ROWID = cmj.chat_id
    JOIN message m ON cmj.message_id = m.ROWID
    WHERE 1=1
    GROUP BY c.chat_identifier
    ORDER BY last_date DESC
""").fetchall()

conn.close()

print(f"{'Identifier':<30} {'Display Name':<25} {'Messages':>8}")
print("-" * 66)
for r in rows:
    print(f"{r[0]:<30} {(r[1] or '(unnamed)'):<25} {r[2]:>8}")
