import sqlite3
con = sqlite3.connect("vouchers.db")
con.execute(
    "INSERT OR IGNORE INTO vouchers (code, amount, store) VALUES (?, ?, ?)",
    ("91098085941400300563", 200, "שופרסל שלי נווה הדרים"),
)
con.commit()
rows = con.execute("SELECT id, code, amount, store, status FROM vouchers").fetchall()
con.close()
print(f"Inserted. DB now has {len(rows)} row(s):")
for r in rows:
    print(r)
