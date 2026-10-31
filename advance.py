"""ParkAI - Advance reservation (time-window), Early-arrival allocation, Dynamic pricing.

Capacity lot-level hi track hota hai (koi numbered slot nahi). Har booking ek time interval
[start_time, end_time) ke liye `slots` (default 1) capacity leti hai. Kisi bhi interval me
overlapping bookings ka peak lot ki capacity se zyada nahi ho sakta.
"""
import hmac
import secrets
from datetime import datetime, timedelta
from math import radians, sin, cos, asin, sqrt

from flask import Blueprint, jsonify, request, render_template

from database import (get_conn, get_tx_conn, tx_begin, tx_commit, tx_rollback,
                      expire_old, reserved_counts, FMT)

bp = Blueprint("advance", __name__)

# ===================== CONFIG (sab yahin se badal sakte ho) =====================
# (kitne minute pehle tak, price Rs per booking, label)  -> upar se neeche check hota hai
PRICING_TIERS = [
    (60, 40, "Within 1 hour"),
    (24 * 60, 25, "More than 1 hour, up to 1 day"),
    (5 * 24 * 60, 20, "More than 1 day, up to 5 days"),
]
MAX_ADVANCE_MIN = PRICING_TIERS[-1][0]   # 5 din
MIN_LEAD_MIN = 1                          # start time kam se kam abhi se 1 min baad
MIN_DURATION_MIN = 15
MAX_DURATION_MIN = 12 * 60
LIVE_OCCUPANCY_HORIZON_MIN = 60           # jo booking is window me shuru ho, usme abhi ki gaadiyan bhi count hongi
EARLY_WINDOW_MIN = 180                    # start se kitna pehle "I'm Here Early" allowed
NEARBY_RADIUS_M = 3000                    # early transfer ke liye nearby lot ki max doori
MAX_ACTIVE_PER_PHONE = 5
CANCEL_FEE_PCT = 20                       # existing reservations jaisa hi
# ================================================================================


def err(msg, code=400, **extra):
    return jsonify({"error": msg, **extra}), code


# ---------------------------- DB setup (safe migration) ----------------------------
def init_advance_db():
    conn = get_conn()
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS advance_bookings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT UNIQUE NOT NULL,
        token TEXT NOT NULL,
        lot_id INTEGER NOT NULL,
        original_lot_id INTEGER NOT NULL,
        user_name TEXT NOT NULL,
        phone TEXT NOT NULL,
        vehicle_no TEXT,
        slots INTEGER NOT NULL DEFAULT 1,
        start_time TEXT NOT NULL,
        end_time TEXT NOT NULL,
        orig_start_time TEXT NOT NULL,
        amount INTEGER NOT NULL,
        price_tier TEXT,
        status TEXT NOT NULL DEFAULT 'CONFIRMED',
        created_at TEXT NOT NULL,
        paid_at TEXT,
        payment_ref TEXT,
        early_status TEXT NOT NULL DEFAULT 'NONE',
        early_at TEXT,
        early_message TEXT,
        cancel_reason TEXT,
        cancelled_at TEXT,
        refund_amount INTEGER NOT NULL DEFAULT 0
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS early_queue (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        booking_id INTEGER NOT NULL,
        joined_at TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'WAITING',
        allocated_at TEXT,
        note TEXT
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_adv_lot ON advance_bookings(lot_id, status, start_time, end_time)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_adv_phone ON advance_bookings(phone, status)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_queue ON early_queue(status, id)")
    conn.commit()
    conn.close()


# ---------------------------- helpers ----------------------------
def fmt(dt):
    return dt.strftime(FMT)


def parse_ts(s):
    return datetime.strptime(s, FMT)


def now_dt():
    return datetime.now().replace(microsecond=0)


def new_code():
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "AD-" + "".join(secrets.choice(alphabet) for _ in range(6))


def distance_m(lat1, lng1, lat2, lng2):
    R = 6371000
    p1, p2 = radians(lat1), radians(lat2)
    dp, dl = p2 - p1, radians(lng2 - lng1)
    a = sin(dp / 2) ** 2 + cos(p1) * cos(p2) * sin(dl / 2) ** 2
    return 2 * R * asin(sqrt(a))


def parse_interval(date_s, start_s, end_s):
    """Date + From + To -> (start_dt, end_dt). ValueError me user-friendly message."""
    try:
        s = datetime.strptime(f"{date_s} {start_s}", "%Y-%m-%d %H:%M")
        e = datetime.strptime(f"{date_s} {end_s}", "%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        raise ValueError("Date, start time aur end time sahi bharo")
    if e <= s:
        raise ValueError("End time start time ke baad ka hona chahiye")
    dur = (e - s).total_seconds() / 60
    if dur < MIN_DURATION_MIN:
        raise ValueError(f"Kam se kam {MIN_DURATION_MIN} minute ki booking karo")
    if dur > MAX_DURATION_MIN:
        raise ValueError(f"Ek booking max {MAX_DURATION_MIN // 60} ghante ki ho sakti hai")
    now = datetime.now()
    if s < now + timedelta(minutes=MIN_LEAD_MIN):
        raise ValueError("Start time aane wale samay ka hona chahiye")
    if s > now + timedelta(minutes=MAX_ADVANCE_MIN):
        raise ValueError(f"Sirf {MAX_ADVANCE_MIN // 1440} din tak advance booking ho sakti hai")
    return s, e


def price_for(start_dt, now=None):
    """Backend pricing: booking start hone me kitna time bacha hai uske hisaab se."""
    now = now or datetime.now()
    mins = (start_dt - now).total_seconds() / 60
    for limit, price, label in PRICING_TIERS:
        if mins <= limit:
            return price, label
    return None, None


def check_payment(d):
    """Existing demo gateway ke wahi rules (UPI / CARD). (msg, code) return karta hai fail par."""
    method = d.get("method")
    if method == "UPI":
        upi = (d.get("upi_id") or "").strip().lower()
        if "@" not in upi or len(upi) < 5:
            return "Sahi UPI ID likho (jaise name@upi)", 400
        if upi == "fail@upi":
            return "Bank ne payment decline kar diya. Dobara try karo.", 402
    elif method == "CARD":
        num = (d.get("card_number") or "").replace(" ", "")
        cvv = (d.get("cvv") or "").strip()
        if not (num.isdigit() and len(num) == 16 and cvv.isdigit() and len(cvv) == 3):
            return "Card details galat hain", 400
        if num.endswith("0000"):
            return "Card decline ho gaya.", 402
    else:
        return "Payment method chuno", 400
    return None


def peak_load(conn, lot, start, end, exclude_id=None):
    """[start, end) me is lot ka maximum load (bookings ka peak + abhi ki live gaadiyan agar window paas hai)."""
    s, e = fmt(start), fmt(end)
    q = ("SELECT start_time, end_time, slots FROM advance_bookings "
         "WHERE lot_id=? AND status='CONFIRMED' AND start_time < ? AND end_time > ?")
    args = [lot["id"], e, s]
    if exclude_id is not None:
        q += " AND id != ?"
        args.append(exclude_id)
    rows = conn.execute(q, args).fetchall()
    points = {s} | {r["start_time"] for r in rows if r["start_time"] > s}
    peak = 0
    for t in points:  # load sirf kisi booking ke start par badhta hai, isliye wahi points check karne kaafi hain
        peak = max(peak, sum(r["slots"] for r in rows if r["start_time"] <= t < r["end_time"]))
    base = 0
    if start < datetime.now() + timedelta(minutes=LIVE_OCCUPANCY_HORIZON_MIN):
        base = lot["current_count"] + reserved_counts(conn).get(lot["id"], 0)
    return peak + base


def free_for(conn, lot, start, end, exclude_id=None):
    return lot["capacity"] - peak_load(conn, lot, start, end, exclude_id)


def get_by_code(conn, code):
    return conn.execute("SELECT * FROM advance_bookings WHERE code=?", (code,)).fetchone()


def token_ok(row, t):
    return row is not None and hmac.compare_digest(row["token"].encode(), (t or "").encode())


def view_status(b):
    if b["status"] == "CONFIRMED" and b["end_time"] < fmt(datetime.now()):
        return "COMPLETED"
    return b["status"]


def detail(conn, b):
    now = datetime.now()
    lot = conn.execute("SELECT name FROM lots WHERE id=?", (b["lot_id"],)).fetchone()
    olot = conn.execute("SELECT name FROM lots WHERE id=?", (b["original_lot_id"],)).fetchone()
    status = view_status(b)
    orig_start = parse_ts(b["orig_start_time"])
    can_early = (status == "CONFIRMED" and b["early_status"] == "NONE" and now < orig_start
                 and (orig_start - now) <= timedelta(minutes=EARLY_WINDOW_MIN))
    pos = None
    if b["early_status"] == "WAITING":
        q = conn.execute("SELECT id FROM early_queue WHERE booking_id=? AND status='WAITING'", (b["id"],)).fetchone()
        if q:
            pos = conn.execute("SELECT COUNT(*) FROM early_queue WHERE status='WAITING' AND id<=?",
                               (q["id"],)).fetchone()[0]
    parked = b["early_status"] in ("ARRIVED_SAME", "TRANSFERRED")
    return {
        "code": b["code"], "lot_id": b["lot_id"], "lot_name": lot["name"] if lot else "?",
        "original_lot_id": b["original_lot_id"], "original_lot_name": olot["name"] if olot else "?",
        "user_name": b["user_name"], "vehicle_no": b["vehicle_no"], "slots": b["slots"],
        "date": b["orig_start_time"][:10], "start": b["start_time"][11:16], "end": b["end_time"][11:16],
        "booked_date": b["orig_start_time"][:10], "booked_start": b["orig_start_time"][11:16],
        "amount": b["amount"], "price_tier": b["price_tier"], "status": status,
        "early_status": b["early_status"], "early_message": b["early_message"],
        "queue_position": pos, "refund_amount": b["refund_amount"],
        "created_at": b["created_at"], "payment_ref": b["payment_ref"],
        "can_cancel": status == "CONFIRMED" and not parked,
        "can_early": can_early,
        "can_modify": status == "CONFIRMED" and b["early_status"] == "NONE" and now < orig_start,
    }


def nearby_lots(conn, lot):
    """Is lot ke nazdeek ke baaki lots (doori ke hisaab se sorted)."""
    out = []
    for r in conn.execute("SELECT * FROM lots WHERE id != ?", (lot["id"],)).fetchall():
        if None in (lot["lat"], lot["lng"], r["lat"], r["lng"]):
            continue
        d = distance_m(lot["lat"], lot["lng"], r["lat"], r["lng"])
        if d <= NEARBY_RADIUS_M:
            out.append((d, r))
    out.sort(key=lambda x: x[0])
    return out


def try_allocate_early(conn, b, now):
    """Pehle same lot, phir nearby lots. (lot_row, 'SAME'|'TRANSFER', distance) ya None."""
    end = parse_ts(b["end_time"])
    own = conn.execute("SELECT * FROM lots WHERE id=?", (b["lot_id"],)).fetchone()
    if own is None:
        return None
    if free_for(conn, own, now, end, exclude_id=b["id"]) >= b["slots"]:
        return own, "SAME", 0
    for d, lot in nearby_lots(conn, own):
        if free_for(conn, lot, now, end) >= b["slots"]:
            return lot, "TRANSFER", d
    return None


def apply_allocation(conn, b, lot, kind, dist, now, via_queue=False):
    ts = fmt(now)
    if kind == "SAME":
        msg = f"{lot['name']} me early parking allowed hai. Aap abhi park kar sakte ho."
        conn.execute("UPDATE advance_bookings SET start_time=?, early_status='ARRIVED_SAME', early_at=?, "
                     "early_message=? WHERE id=?", (ts, ts, msg, b["id"]))
    else:
        old = conn.execute("SELECT name FROM lots WHERE id=?", (b["lot_id"],)).fetchone()["name"]
        msg = (f"{old} full tha. Aapki booking {lot['name']} me shift kar di gayi "
               f"(~{round(dist)} m door). Baaki time wahin valid hai.")
        conn.execute("UPDATE advance_bookings SET lot_id=?, start_time=?, early_status='TRANSFERRED', "
                     "early_at=?, early_message=? WHERE id=?", (lot["id"], ts, ts, msg, b["id"]))
    if via_queue:
        msg = "Waiting queue se allocation mila: " + msg
        conn.execute("UPDATE advance_bookings SET early_message=? WHERE id=?", (msg, b["id"]))
    conn.execute("UPDATE early_queue SET status='ALLOCATED', allocated_at=? WHERE booking_id=? AND status='WAITING'",
                 (ts, b["id"]))
    return msg


def process_queue(conn):
    """FCFS: queue me pehle aaye ko pehle capacity. Transaction ke andar chalao."""
    now = now_dt()
    rows = conn.execute("""SELECT q.id AS qid, b.* FROM early_queue q
                           JOIN advance_bookings b ON b.id = q.booking_id
                           WHERE q.status='WAITING' ORDER BY q.id""").fetchall()
    for r in rows:
        valid = (r["status"] == "CONFIRMED" and r["early_status"] == "WAITING"
                 and now < parse_ts(r["orig_start_time"]) and now < parse_ts(r["end_time"]))
        if not valid:
            conn.execute("UPDATE early_queue SET status='EXPIRED', note='booking/early window valid nahi' WHERE id=?",
                         (r["qid"],))
            if r["status"] == "CONFIRMED" and r["early_status"] == "WAITING":
                conn.execute("UPDATE advance_bookings SET early_status='NONE', early_message=? WHERE id=?",
                             ("Early waiting khatam: aapki original booking time par valid hai.", r["id"]))
            continue
        res = try_allocate_early(conn, r, now)
        if res:
            lot, kind, dist = res
            apply_allocation(conn, r, lot, kind, dist, now, via_queue=True)


def run_tx(work):
    """Har request: write-lock -> expire -> queue process -> kaam -> commit."""
    conn = get_tx_conn()
    try:
        tx_begin(conn)
        expire_old(conn)
        process_queue(conn)
        resp = work(conn)
        tx_commit(conn)
        return resp
    except Exception:
        tx_rollback(conn)
        raise
    finally:
        conn.close()


# ---------------------------- PAGE ----------------------------
@bp.route("/advance")
def advance_page():
    return render_template("advance.html")


# ---------------------------- APIs ----------------------------
@bp.route("/api/advance/config")
def config():
    return jsonify({
        "tiers": [{"max_minutes": m, "price": p, "label": l} for m, p, l in PRICING_TIERS],
        "max_days": MAX_ADVANCE_MIN // 1440, "min_duration_min": MIN_DURATION_MIN,
        "max_duration_min": MAX_DURATION_MIN, "early_window_min": EARLY_WINDOW_MIN,
        "cancel_fee_pct": CANCEL_FEE_PCT,
    })


@bp.route("/api/advance/quote")
def quote():
    try:
        lot_id = int(request.args.get("lot_id"))
    except (TypeError, ValueError):
        return err("Parking lot chuno")
    try:
        start, end = parse_interval(request.args.get("date"), request.args.get("start_time"),
                                    request.args.get("end_time"))
    except ValueError as e:
        return err(str(e))

    def work(conn):
        lot = conn.execute("SELECT * FROM lots WHERE id=?", (lot_id,)).fetchone()
        if lot is None:
            return err("Lot nahi mila", 404)
        price, tier = price_for(start)
        free = max(0, free_for(conn, lot, start, end))
        ok = free >= 1
        return jsonify({"price": price, "tier": tier, "available": ok, "free_slots": free,
                        "duration_min": int((end - start).total_seconds() // 60),
                        "message": None if ok else "Is time slot me ye parking lot full hai. Alag time ya lot chuno."})
    return run_tx(work)


@bp.route("/api/advance/book", methods=["POST"])
def book():
    d = request.get_json(silent=True) or {}
    try:
        lot_id = int(d.get("lot_id"))
    except (TypeError, ValueError):
        return err("Parking lot chuno")
    name = (d.get("user_name") or "").strip()
    phone = (d.get("phone") or "").strip()
    vehicle = (d.get("vehicle_no") or "").strip().upper()
    if len(name) < 2:
        return err("Naam likho")
    if not (phone.isdigit() and len(phone) == 10):
        return err("10 digit ka phone number likho")
    try:
        start, end = parse_interval(d.get("date"), d.get("start_time"), d.get("end_time"))
    except ValueError as e:
        return err(str(e))
    pay_err = check_payment(d)
    if pay_err:
        return err(*pay_err)

    def work(conn):
        lot = conn.execute("SELECT * FROM lots WHERE id=?", (lot_id,)).fetchone()
        if lot is None:
            return err("Lot nahi mila", 404)
        active = conn.execute(
            "SELECT COUNT(*) FROM advance_bookings WHERE phone=? AND status='CONFIRMED' AND end_time > ?",
            (phone, fmt(datetime.now()))).fetchone()[0]
        if active >= MAX_ACTIVE_PER_PHONE:
            return err(f"Ek phone number par max {MAX_ACTIVE_PER_PHONE} active advance bookings ho sakti hain", 409)
        if free_for(conn, lot, start, end) < 1:
            return err("Is time slot me ye parking lot full hai. Alag time ya lot chuno.", 409, lot_full=True)
        now = datetime.now()
        price, tier = price_for(start, now)
        if price is None:
            return err("Booking time valid nahi hai")
        code, token = new_code(), secrets.token_urlsafe(16)
        conn.execute(
            """INSERT INTO advance_bookings (code, token, lot_id, original_lot_id, user_name, phone, vehicle_no,
               slots, start_time, end_time, orig_start_time, amount, price_tier, status, created_at, paid_at,
               payment_ref) VALUES (?,?,?,?,?,?,?,1,?,?,?,?,?, 'CONFIRMED', ?, ?, ?)""",
            (code, token, lot_id, lot_id, name, phone, vehicle, fmt(start), fmt(end), fmt(start),
             price, tier, fmt(now), fmt(now), "PAY" + secrets.token_hex(4).upper()))
        b = get_by_code(conn, code)
        return jsonify({"ok": True, "code": code, "token": token, "booking": detail(conn, b)})
    return run_tx(work)


@bp.route("/api/advance/mine", methods=["POST"])
def mine():
    items = (request.get_json(silent=True) or {}).get("items") or []

    def work(conn):
        out = []
        for it in items[:50]:
            b = get_by_code(conn, str((it or {}).get("code", "")))
            if token_ok(b, str((it or {}).get("token", ""))):
                out.append(detail(conn, b))
        out.sort(key=lambda x: (x["date"], x["start"]), reverse=True)
        return jsonify(out)
    return run_tx(work)


@bp.route("/api/advance/<code>")
def get_one(code):
    t = request.args.get("t", "")

    def work(conn):
        b = get_by_code(conn, code)
        if not token_ok(b, t):
            return err("Booking nahi mili", 404)
        return jsonify(detail(conn, b))
    return run_tx(work)


@bp.route("/api/advance/<code>/cancel", methods=["POST"])
def cancel(code):
    d = request.get_json(silent=True) or {}

    def work(conn):
        b = get_by_code(conn, code)
        if not token_ok(b, d.get("t")):
            return err("Booking nahi mili", 404)
        st = view_status(b)
        if st != "CONFIRMED":
            return err(f"Booking {st} hai, cancel nahi ho sakti", 409)
        if b["early_status"] in ("ARRIVED_SAME", "TRANSFERRED"):
            return err("Aap early park kar chuke ho, ab cancel nahi ho sakti", 409)
        refund = b["refund_amount"] + b["amount"] * (100 - CANCEL_FEE_PCT) // 100
        ts = fmt(datetime.now())
        conn.execute("UPDATE advance_bookings SET status='CANCELLED', cancel_reason='cancelled by user', "
                     "cancelled_at=?, refund_amount=?, early_status='NONE' WHERE id=?", (ts, refund, b["id"]))
        conn.execute("UPDATE early_queue SET status='CANCELLED' WHERE booking_id=? AND status='WAITING'", (b["id"],))
        process_queue(conn)  # jo capacity abhi free hui, waiting logon ko FCFS me mil jaye
        return jsonify({"ok": True, "refund": refund})
    return run_tx(work)


@bp.route("/api/advance/<code>/early", methods=["POST"])
def early(code):
    d = request.get_json(silent=True) or {}
    join_queue = bool(d.get("join_queue"))

    def work(conn):
        b = get_by_code(conn, code)
        if not token_ok(b, d.get("t")):
            return err("Booking nahi mili", 404)
        if view_status(b) != "CONFIRMED":
            return err("Ye booking active nahi hai", 409)
        if b["early_status"] in ("ARRIVED_SAME", "TRANSFERRED"):
            return err("Early arrival pehle hi allow ho chuka hai", 409)
        now = now_dt()
        orig = parse_ts(b["orig_start_time"])
        if now >= orig:
            return err("Aapka booking time shuru ho chuka hai, early arrival ki zarurat nahi.", 409)
        if orig - now > timedelta(minutes=EARLY_WINDOW_MIN):
            return err(f"'I'm Here Early' booking se max {EARLY_WINDOW_MIN} minute pehle tak hi use hota hai", 409)
        if b["early_status"] == "WAITING":  # process_queue pehle hi try kar chuka, abhi bhi capacity nahi
            return jsonify({"ok": True, "outcome": "QUEUED", "message": "Aap pehle se waiting queue me ho.",
                            "booking": detail(conn, b)})
        res = try_allocate_early(conn, b, now)
        if res:
            lot, kind, dist = res
            msg = apply_allocation(conn, b, lot, kind, dist, now)
            b = get_by_code(conn, code)
            return jsonify({"ok": True, "outcome": "SAME_LOT" if kind == "SAME" else "TRANSFERRED",
                            "message": msg, "booking": detail(conn, b)})
        if not join_queue:
            return jsonify({"ok": False, "outcome": "NO_CAPACITY", "offer_queue": True,
                            "message": "Abhi koi nazdeeki lot me jagah nahi hai. Kya aap early-arrival waiting queue "
                                       "(pehle aao pehle pao) me judna chahoge? Aapki original booking safe rahegi."})
        conn.execute("INSERT INTO early_queue (booking_id, joined_at, status) VALUES (?,?, 'WAITING')",
                     (b["id"], fmt(now)))
        conn.execute("UPDATE advance_bookings SET early_status='WAITING', early_message=? WHERE id=?",
                     ("Aap early-arrival waiting queue me ho. Jagah milte hi yahan update aa jayega.", b["id"]))
        b = get_by_code(conn, code)
        return jsonify({"ok": True, "outcome": "QUEUED", "message": "Waiting queue me add ho gaye.",
                        "booking": detail(conn, b)})
    return run_tx(work)


@bp.route("/api/advance/<code>/early/leave", methods=["POST"])
def early_leave(code):
    d = request.get_json(silent=True) or {}

    def work(conn):
        b = get_by_code(conn, code)
        if not token_ok(b, d.get("t")):
            return err("Booking nahi mili", 404)
        if b["early_status"] == "WAITING":
            conn.execute("UPDATE early_queue SET status='CANCELLED' WHERE booking_id=? AND status='WAITING'", (b["id"],))
            conn.execute("UPDATE advance_bookings SET early_status='NONE', early_message=? WHERE id=?",
                         ("Waiting queue chhod di. Original booking valid hai.", b["id"]))
        b = get_by_code(conn, code)
        return jsonify({"ok": True, "booking": detail(conn, b)})
    return run_tx(work)


@bp.route("/api/advance/<code>/modify", methods=["POST"])
def modify(code):
    """Booking ka time badlo: capacity dobara check + price naye start time ke hisaab se recalculate."""
    d = request.get_json(silent=True) or {}
    try:
        start, end = parse_interval(d.get("date"), d.get("start_time"), d.get("end_time"))
    except ValueError as e:
        return err(str(e))
    preview = bool(d.get("preview"))

    def work(conn):
        b = get_by_code(conn, code)
        if not token_ok(b, d.get("t")):
            return err("Booking nahi mili", 404)
        if view_status(b) != "CONFIRMED" or b["early_status"] != "NONE":
            return err("Is booking ka time abhi change nahi ho sakta", 409)
        if datetime.now() >= parse_ts(b["orig_start_time"]):
            return err("Booking shuru ho chuki hai, time change nahi ho sakta", 409)
        lot = conn.execute("SELECT * FROM lots WHERE id=?", (b["lot_id"],)).fetchone()
        if free_for(conn, lot, start, end, exclude_id=b["id"]) < b["slots"]:
            return err("Naye time me ye parking lot full hai", 409, lot_full=True)
        price, tier = price_for(start)
        diff = price - b["amount"]
        info = {"old_amount": b["amount"], "new_amount": price, "difference": diff, "tier": tier}
        if preview:
            return jsonify({"ok": True, "preview": True, **info})
        if diff > 0:
            pay_err = check_payment(d)
            if pay_err:
                return err(*pay_err, **info)
        refund = b["refund_amount"] + (-diff if diff < 0 else 0)
        conn.execute("UPDATE advance_bookings SET start_time=?, end_time=?, orig_start_time=?, amount=?, "
                     "price_tier=?, refund_amount=? WHERE id=?",
                     (fmt(start), fmt(end), fmt(start), price, tier, refund, b["id"]))
        b = get_by_code(conn, code)
        return jsonify({"ok": True, **info, "booking": detail(conn, b)})
    return run_tx(work)