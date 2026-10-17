"""联网实测：用临时库从 FastF1 拉 2026/2027 赛历，打印每站与样例日期的时期判断。"""
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hermes import calendar as cal  # noqa: E402
from hermes import db  # noqa: E402

with tempfile.TemporaryDirectory() as tmp:
    conn = db.connect(Path(tmp) / "t.db")
    print("第一次 refresh 变动：", cal.refresh(conn, [2026, 2027]))
    for e in cal.events(conn, 2026):
        race = next((s["start_bj"] for s in e["sessions"] if s["name"] == "Race"), None)
        print(f"R{e['round']:>2} {e['name_en']:<28} {e['location']:<16} 正赛 {e['event_date']} {e['format']:<18} 北京 {race}")
    n27 = len(cal.events(conn, 2027))
    print("2027 有数据：", f"是（{n27} 站）" if n27 else "否")
    for d in ("2026-09-29", "2026-08-05", "2026-04-15", "2026-12-20", "2026-01-10"):
        day = date.fromisoformat(d)
        print(d, cal.period(conn, day), cal.gp_label(conn, day))
    print("第二次 refresh 变动：", cal.refresh(conn, [2026, 2027]))
    conn.close()
