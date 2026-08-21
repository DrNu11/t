"""Inspect trident.db schema and row counts for replay panel design."""
import sqlite3
import sys

DB = r"C:\Users\ASUS\Desktop\Trident_Agent_MVP\backend\trident_event_bus.db"

con = sqlite3.connect(DB)
cur = con.cursor()
cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
tables = [r[0] for r in cur.fetchall()]
print("=== TABLES ===")
for t in tables:
    print(" ", t)

for t in tables:
    cur.execute(f"PRAGMA table_info({t})")
    cols = cur.fetchall()
    cur.execute(f"SELECT COUNT(*) FROM {t}")
    n = cur.fetchone()[0]
    print(f"\n=== {t} ({len(cols)} cols, {n} rows) ===")
    for c in cols:
        print(f"  {c[1]:<28} {c[2]}")

# 看一下 signals 表有没有 EXECUTED 状态和 AI 归因
try:
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%signal%'")
    sig_tables = [r[0] for r in cur.fetchall()]
    for st in sig_tables:
        print(f"\n--- {st} 状态分布 ---")
        cur.execute(f"SELECT analysis_status, COUNT(*) FROM {st} GROUP BY analysis_status")
        for r in cur.fetchall():
            print(f"  {r[0]}: {r[1]}")
        print(f"--- {st} 信号样本 (前 3 行) ---")
        cur.execute(f"SELECT * FROM {st} LIMIT 3")
        for row in cur.fetchall():
            print(f"  {row}")
except Exception as e:
    print("err:", e)

con.close()