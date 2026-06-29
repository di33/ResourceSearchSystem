from pathlib import Path
import sqlite3

DB_PATH = Path(__file__).resolve().parents[1] / "data" / "databases" / "pipeline.db"
conn = sqlite3.connect(DB_PATH)
rows = conn.execute('SELECT process_state, COUNT(*) as cnt FROM resource_task GROUP BY process_state ORDER BY cnt DESC').fetchall()
total = 0
for state, cnt in rows:
	print(f'  {state}: {cnt}')
	total += cnt
print(f'  --------')
print(f'  总计: {total}')
conn.close()
