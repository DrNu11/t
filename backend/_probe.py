import sqlite3

conn = sqlite3.connect("trident_event_bus.db")
conn.row_factory = sqlite3.Row
print("settled_with_entry:", conn.execute("SELECT COUNT(*) FROM ai_decisions WHERE settled=1 AND entry_price>0").fetchone()[0])
for row in conn.execute("SELECT COALESCE(agent_model_id,'') AS m, COUNT(*) AS c FROM ai_decisions WHERE settled=1 AND entry_price>0 GROUP BY m"):
    print("model:", repr(row["m"]), row["c"])
for row in conn.execute("SELECT id, target_asset, suggested_action, is_correct, forward_pnl, paper_trading_run_id, agent_model_id FROM ai_decisions WHERE settled=1 AND entry_price>0 ORDER BY id DESC LIMIT 10"):
    print(dict(row))
print("run11_decisions:")
for row in conn.execute("SELECT id, suggested_action, target_asset, evidence_confidence, trade_gate_reason, entry_price, paper_trading_run_id FROM ai_decisions WHERE paper_trading_run_id IS NOT NULL ORDER BY id DESC LIMIT 8"):
    print(dict(row))
