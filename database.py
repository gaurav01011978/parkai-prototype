import sqlite3
from datetime import datetime

DB_NAME = "parkai.db"
FMT = "%Y-%m-%d %H:%M:%S"


def now_str():
    return datetime.now().strftime(FMT)


def get_conn():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn


def get_tx_conn():
    """Reservation/payment ke liye: manual transaction. Writers ek-ek karke chalte hain."""
    conn = sqlite3.connect(DB_NAME, timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def tx_begin(conn):
    conn.execute("BEGIN IMMEDIATE")   # yahin write-lock mil jaata hai


def tx_commit(conn):
    conn.execute("COMMIT")


def tx_rollback(conn):
    try:
        conn.execute("ROLLBACK")
    except sqlite3.OperationalError:
        pass


def init_db():
    conn = get_conn()
    c = conn.cursor()

    c.execute("""CREATE TABLE IF NOT EXISTS lots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        capacity INTEGER NOT NULL,
        current_count INTEGER NOT NULL DEFAULT 0,
        lat REAL,
        lng REAL,
        last_updated TEXT
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        lot_id INTEGER NOT NULL,
        action TEXT NOT NULL,
        operator TEXT,
        timestamp TEXT NOT NULL,
        undone INTEGER NOT NULL DEFAULT 0
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        lot_id INTEGER NOT NULL,
        hour INTEGER NOT NULL,
        weekday INTEGER NOT NULL,
        occupancy_percent INTEGER NOT NULL
    )""")

    # ---------- reservation system ----------
    c.execute("""CREATE TABLE IF NOT EXISTS reservations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT UNIQUE NOT NULL,
        token TEXT NOT NULL,
        lot_id INTEGER NOT NULL,
        user_name TEXT NOT NULL,
        phone TEXT NOT NULL,
        vehicle_no TEXT,
        slots INTEGER NOT NULL,
        hold_minutes INTEGER NOT NULL,
        amount INTEGER NOT NULL,
        status TEXT NOT NULL,
        created_at TEXT NOT NULL,
        pay_by TEXT NOT NULL,
        expires_at TEXT,
        paid_at TEXT,
        payment_ref TEXT,
        checkin_at TEXT,
        checkin_by TEXT,
        otp_fails INTEGER NOT NULL DEFAULT 0,
        cancel_reason TEXT,
        refund_amount INTEGER NOT NULL DEFAULT 0
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS res_slots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        reservation_id INTEGER NOT NULL,
        lot_id INTEGER NOT NULL,
        bay_no INTEGER NOT NULL,
        active INTEGER NOT NULL DEFAULT 1
    )""")
    # DB-level guarantee: ek lot ka ek bay do active booking me nahi ja sakta
    c.execute("""CREATE UNIQUE INDEX IF NOT EXISTS uq_active_bay
                 ON res_slots(lot_id, bay_no) WHERE active = 1""")

    c.execute("""CREATE TABLE IF NOT EXISTS operators (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        pin TEXT NOT NULL,
        lot_id INTEGER NOT NULL
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS security_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        type TEXT NOT NULL,
        severity TEXT NOT NULL,
        lot_id INTEGER,
        operator TEXT,
        reservation_code TEXT,
        detail TEXT
    )""")

    # Demo lots
    count = c.execute("SELECT COUNT(*) FROM lots").fetchone()[0]
    if count == 0:
        now = now_str()
        demo_lots = [
            ("Lot A", 100, 74, 28.5514, 77.2543),  # 26 free
            ("Lot B", 60, 52, 28.5484, 77.2536),   # 8 free
            ("Lot C", 150, 110, 28.5459, 77.2490), # 40 free
        ]
        for name, cap, cur, lat, lng in demo_lots:
            c.execute(
                "INSERT INTO lots (name, capacity, current_count, lat, lng, last_updated) VALUES (?,?,?,?,?,?)",
                (name, cap, cur, lat, lng, now),
            )

    # Demo operators (har operator ek lot se bandha hai)
    if c.execute("SELECT COUNT(*) FROM operators").fetchone()[0] == 0:
        c.executemany(
            "INSERT INTO operators (id, name, pin, lot_id) VALUES (?,?,?,?)",
            [("Op-12", "Operator 12", "1234", 1),
             ("Op-07", "Operator 07", "5678", 2),
             ("Op-03", "Operator 03", "9012", 3)],
        )

    conn.commit()
    conn.close()


# ---------- helpers (transaction ke andar chalao) ----------
def expire_old(conn):
    """Time khatam hui bookings ke slots wapas free kar do."""
    now = now_str()
    conn.execute("""UPDATE res_slots SET active=0 WHERE active=1 AND reservation_id IN (
        SELECT id FROM reservations
        WHERE (status='PENDING_PAYMENT' AND pay_by < ?)
           OR (status IN ('CONFIRMED','LOCKED') AND expires_at < ?))""", (now, now))
    conn.execute("""UPDATE reservations SET status='EXPIRED', cancel_reason='payment time over'
                    WHERE status='PENDING_PAYMENT' AND pay_by < ?""", (now,))
    conn.execute("""UPDATE reservations SET status='NO_SHOW'
                    WHERE status IN ('CONFIRMED','LOCKED') AND expires_at < ?""", (now,))


def reserved_counts(conn):
    rows = conn.execute(
        "SELECT lot_id, COUNT(*) AS c FROM res_slots WHERE active=1 GROUP BY lot_id").fetchall()
    return {r["lot_id"]: r["c"] for r in rows}


def log_event(conn, type_, severity, lot_id=None, operator=None, code=None, detail=""):
    conn.execute(
        "INSERT INTO security_events (ts, type, severity, lot_id, operator, reservation_code, detail) VALUES (?,?,?,?,?,?,?)",
        (now_str(), type_, severity, lot_id, operator, code, detail))