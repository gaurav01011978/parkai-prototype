import random
from database import get_conn, init_db

init_db()
conn = get_conn()

# Purani history saaf, taaki dobara chalane par duplicate na ho
conn.execute("DELETE FROM history")

lot_ids = [r["id"] for r in conn.execute("SELECT id FROM lots").fetchall()]

# Ghante ke hisaab se kitni bheed hoti hai (12 baje rush)
base_pattern = {
    6: 10, 7: 20, 8: 30, 9: 45, 10: 60, 11: 80, 12: 90, 13: 85,
    14: 60, 15: 50, 16: 55, 17: 65, 18: 70, 19: 60, 20: 40, 21: 25, 22: 15,
}

for lot_id in lot_ids:
    lot_bias = random.randint(-10, 10)  # har lot ka thoda alag nature
    for day in range(28):               # 28 din ka data
        weekday = day % 7
        for hour, base in base_pattern.items():
            value = base + lot_bias + random.randint(-8, 8)
            value = max(0, min(100, value))
            conn.execute(
                "INSERT INTO history (lot_id, hour, weekday, occupancy_percent) VALUES (?,?,?,?)",
                (lot_id, hour, weekday, value),
            )

conn.commit()
conn.close()
print("History seed ho gayi!")