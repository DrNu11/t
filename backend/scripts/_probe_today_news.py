import datetime
import sqlite3
from pathlib import Path

p = Path(r"c:\Users\ASUS\Desktop\Trident_Agent_MVP\backend\trident_event_bus.db")
print("exists", p.exists(), "size", p.stat().st_size if p.exists() else 0)
c = sqlite3.connect(p)
c.row_factory = sqlite3.Row
row = c.execute("select * from paper_trading_settings where id=1").fetchone()
print("settings", dict(row) if row else None)
today = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).strftime("%Y-%m-%d")
print("today", today)
print("raw_total", c.execute("select count(*) from raw_news").fetchone()[0])
print("raw_today_prefix", c.execute("select count(*) from raw_news where substr(timestamp,1,10)=?", (today,)).fetchone()[0])
print("raw_today_clean", c.execute("select count(*) from raw_news where is_noise=0 and substr(timestamp,1,10)=?", (today,)).fetchone()[0])
print("raw_today_done", c.execute("select count(*) from raw_news where is_noise=0 and status='DONE' and substr(timestamp,1,10)=?", (today,)).fetchone()[0])
print("by_source", [tuple(r) for r in c.execute("select source, is_noise, count(*) from raw_news where substr(timestamp,1,10)=? group by source,is_noise", (today,))])
print("status_dist", [tuple(r) for r in c.execute("select status, count(*) from raw_news where substr(timestamp,1,10)=? group by status", (today,))])
print("date_dist", [tuple(r) for r in c.execute("select substr(timestamp,1,10), count(*) from raw_news group by 1 order by 1 desc limit 10")])
print("ts_samples")
for r in c.execute("select id, timestamp, ts, status, is_noise, source, substr(content,1,60) as c from raw_news order by id desc limit 12"):
    print(dict(r))
