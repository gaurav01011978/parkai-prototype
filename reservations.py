import hmac
import hashlib
import secrets
import sqlite3
from datetime import datetime, timedelta
from flask import Blueprint, jsonify, request
from database import (get_tx_conn, tx_begin, tx_commit, tx_rollback,
                      expire_old, reserved_counts, log_event, now_str, FMT)

bp = Blueprint("reservations", __name__)

SECRET = b"parkai-demo-secret-change-me"
RATE_PER_SLOT_30MIN = 20      # Rs per slot per 30 min
HOLD_OPTIONS = [30, 60, 90, 120]
MAX_SLOTS = 10
MAX_ACTIVE_PER_PHONE = 2      # slot hoarding rokne ke liye
PAY_WINDOW_MIN = 5
CANCEL_FEE_PCT = 20
MAX_OTP_FAILS = 3


def err(msg, code=400):
    return jsonify({"error": msg}), code


def otp_for(code):
    """OTP database me store nahi hota, server ke secret se nikalta hai."""
    d = hmac.new(SECRET, code.encode(), hashlib.sha256).hexdigest()
    return str(int(d, 16) % 1000000).zfill(6)


def new_code():
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "PK-" + "".join(secrets.choice(alphabet) for _ in range(6))


def secs_left(ts):
    if not ts:
        return None
    return int((datetime.strptime(ts, FMT) - datetime.now()).total_seconds())


def bays_of(conn, rid):
    rows = conn.execute(
        "SELECT bay_no FROM res_slots WHERE reservation_id=? ORDER BY bay_no", (rid,)).fetchall()
    return ["R%02d" % r["bay_no"] for r in rows]


def get_by_code(conn, code):
    return conn.execute("SELECT * FROM reservations WHERE code=?", (code,)).fetchone()


def token_ok(row, t):
    return row is not None and hmac.compare_digest(row["token"].encode(), (t or "").encode())


def detail(conn, row, with_otp=False):
    lot = conn.execute("SELECT name FROM lots WHERE id=?", (row["lot_id"],)).fetchone()
    d = {
        "code": row["code"], "lot_id": row["lot_id"], "lot_name": lot["name"] if lot else "?",
        "user_name": row["user_name"], "vehicle_no": row["vehicle_no"], "slots": row["slots"],
        "hold_minutes": row["hold_minutes"], "amount": row["amount"], "status": row["status"],
        "created_at": row["created_at"], "pay_by": row["pay_by"], "expires_at": row["expires_at"],
        "paid_at": row["paid_at"], "payment_ref": row["payment_ref"], "checkin_at": row["checkin_at"],
        "refund_amount": row["refund_amount"], "cancel_reason": row["cancel_reason"],
        "bays": bays_of(conn, row["id"]),
        "seconds_to_pay": secs_left(row["pay_by"]) if row["status"] == "PENDING_PAYMENT" else None,
        "seconds_to_expiry": secs_left(row["expires_at"]) if row["status"] in ("CONFIRMED", "LOCKED") else None,
    }
    if with_otp and row["status"] == "CONFIRMED":
        d["otp"] = otp_for(row["code"])
    return d


# ---------------- USER APIs ----------------
@bp.route("/api/reservations/config")
def config():
    return jsonify({"rate": RATE_PER_SLOT_30MIN, "hold_options": HOLD_OPTIONS, "max_slots": MAX_SLOTS,
                    "pay_window_min": PAY_WINDOW_MIN, "cancel_fee_pct": CANCEL_FEE_PCT})


@bp.route("/api/reservations", methods=["POST"])
def create():
    data = request.get_json(silent=True) or {}
    try:
        lot_id = int(data.get("lot_id"))
        slots = int(data.get("slots"))
        hold = int(data.get("hold_minutes"))
    except (TypeError, ValueError):
        return err("Lot, slots aur hold time sahi bharo")
    name = (data.get("user_name") or "").strip()
    phone = (data.get("phone") or "").strip()
    vehicle = (data.get("vehicle_no") or "").strip().upper()

    if len(name) < 2:
        return err("Naam likho")
    if not (phone.isdigit() and len(phone) == 10):
        return err("10 digit ka phone number likho")
    if not (1 <= slots <= MAX_SLOTS):
        return err(f"Ek booking me 1 se {MAX_SLOTS} slots hi le sakte ho")
    if hold not in HOLD_OPTIONS:
        return err("Hold time galat hai")

    conn = get_tx_conn()
    try:
        tx_begin(conn)          # <-- yahan se koi aur booking tab tak ruki rahegi jab tak ye khatam na ho
        expire_old(conn)
        lot = conn.execute("SELECT * FROM lots WHERE id=?", (lot_id,)).fetchone()
        if lot is None:
            tx_rollback(conn)
            return err("Lot nahi mila", 404)

        active = conn.execute(
            "SELECT COUNT(*) FROM reservations WHERE phone=? AND status IN ('PENDING_PAYMENT','CONFIRMED')",
            (phone,)).fetchone()[0]
        if active >= MAX_ACTIVE_PER_PHONE:
            tx_rollback(conn)
            return err(f"Ek phone number par max {MAX_ACTIVE_PER_PHONE} active bookings ho sakti hain")

        reserved = reserved_counts(conn).get(lot_id, 0)
        available = lot["capacity"] - lot["current_count"] - reserved
        if slots > available:
            tx_rollback(conn)
            return err(f"Sirf {max(available, 0)} slots available hain", 409)

        # sabse chhote khaali bay numbers chuno
        used = {r["bay_no"] for r in conn.execute(
            "SELECT bay_no FROM res_slots WHERE lot_id=? AND active=1", (lot_id,))}
        bays, n = [], 1
        while len(bays) < slots:
            if n not in used:
                bays.append(n)
            n += 1

        now = datetime.now()
        code, token = new_code(), secrets.token_urlsafe(16)
        amount = slots * (hold // 30) * RATE_PER_SLOT_30MIN
        cur = conn.execute(
            """INSERT INTO reservations (code, token, lot_id, user_name, phone, vehicle_no, slots,
               hold_minutes, amount, status, created_at, pay_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (code, token, lot_id, name, phone, vehicle, slots, hold, amount, "PENDING_PAYMENT",
             now.strftime(FMT), (now + timedelta(minutes=PAY_WINDOW_MIN)).strftime(FMT)))
        rid = cur.lastrowid
        for b in bays:
            conn.execute("INSERT INTO res_slots (reservation_id, lot_id, bay_no, active) VALUES (?,?,?,1)",
                         (rid, lot_id, b))
        tx_commit(conn)
    except sqlite3.IntegrityError:
        tx_rollback(conn)
        return err("Ye slot abhi kisi aur ne le liya. Dobara try karo.", 409)
    except Exception:
        tx_rollback(conn)
        raise
    finally:
        conn.close()

    return jsonify({"ok": True, "code": code, "token": token, "amount": amount,
                    "bays": ["R%02d" % b for b in bays],
                    "pay_url": f"/pay/{code}?t={token}"})


@bp.route("/api/reservations/<code>")
def get_one(code):
    t = request.args.get("t", "")
    conn = get_tx_conn()
    try:
        tx_begin(conn)
        expire_old(conn)
        row = get_by_code(conn, code)
        if not token_ok(row, t):
            tx_commit(conn)
            return err("Ticket nahi mila", 404)
        d = detail(conn, row, with_otp=True)
        tx_commit(conn)
    except Exception:
        tx_rollback(conn)
        raise
    finally:
        conn.close()
    return jsonify(d)


@bp.route("/api/reservations/<code>/pay", methods=["POST"])
def pay(code):
    data = request.get_json(silent=True) or {}
    method = data.get("method")

    # ---- DEMO GATEWAY: asli Razorpay yahin lagega ----
    if method == "UPI":
        upi = (data.get("upi_id") or "").strip().lower()
        if "@" not in upi or len(upi) < 5:
            return err("Sahi UPI ID likho (jaise name@upi)")
        if upi == "fail@upi":
            return err("Bank ne payment decline kar diya. Dobara try karo.", 402)
    elif method == "CARD":
        num = (data.get("card_number") or "").replace(" ", "")
        cvv = (data.get("cvv") or "").strip()
        if not (num.isdigit() and len(num) == 16 and cvv.isdigit() and len(cvv) == 3):
            return err("Card details galat hain")
        if num.endswith("0000"):
            return err("Card decline ho gaya.", 402)
    else:
        return err("Payment method chuno")

    conn = get_tx_conn()
    try:
        tx_begin(conn)
        expire_old(conn)
        row = get_by_code(conn, code)
        if not token_ok(row, data.get("t")):
            tx_commit(conn)
            return err("Ticket nahi mila", 404)
        if row["status"] == "CONFIRMED":
            tx_commit(conn)
            return jsonify({"ok": True, "already": True})
        if row["status"] != "PENDING_PAYMENT":
            tx_commit(conn)
            return err("Payment ka time khatam ho gaya, slots release ho chuke hain", 409)
        now = datetime.now()
        conn.execute(
            "UPDATE reservations SET status='CONFIRMED', paid_at=?, payment_ref=?, expires_at=? WHERE id=?",
            (now.strftime(FMT), "PAY" + secrets.token_hex(4).upper(),
             (now + timedelta(minutes=row["hold_minutes"])).strftime(FMT), row["id"]))
        tx_commit(conn)
    except Exception:
        tx_rollback(conn)
        raise
    finally:
        conn.close()
    return jsonify({"ok": True})


@bp.route("/api/reservations/<code>/cancel", methods=["POST"])
def cancel(code):
    data = request.get_json(silent=True) or {}
    conn = get_tx_conn()
    try:
        tx_begin(conn)
        expire_old(conn)
        row = get_by_code(conn, code)
        if not token_ok(row, data.get("t")):
            tx_commit(conn)
            return err("Ticket nahi mila", 404)
        if row["status"] not in ("PENDING_PAYMENT", "CONFIRMED"):
            tx_commit(conn)
            return err(f"Booking {row['status']} hai, cancel nahi ho sakti", 409)
        refund = row["amount"] * (100 - CANCEL_FEE_PCT) // 100 if row["status"] == "CONFIRMED" else 0
        conn.execute("UPDATE reservations SET status='CANCELLED', cancel_reason='cancelled by user', refund_amount=? WHERE id=?",
                     (refund, row["id"]))
        conn.execute("UPDATE res_slots SET active=0 WHERE reservation_id=?", (row["id"],))
        tx_commit(conn)
    except Exception:
        tx_rollback(conn)
        raise
    finally:
        conn.close()
    return jsonify({"ok": True, "refund": refund})


# ---------------- OPERATOR: reserved gaadi check-in ----------------
@bp.route("/api/checkin", methods=["POST"])
def checkin():
    data = request.get_json(silent=True) or {}
    op_id = (data.get("operator_id") or "").strip()
    pin = (data.get("pin") or "").strip()
    code = (data.get("code") or "").strip().upper()
    otp = (data.get("otp") or "").strip()

    conn = get_tx_conn()
    try:
        tx_begin(conn)
        expire_old(conn)

        op = conn.execute("SELECT * FROM operators WHERE id=?", (op_id,)).fetchone()
        if op is None or not hmac.compare_digest(op["pin"].encode(), pin.encode()):
            log_event(conn, "BAD_OPERATOR_LOGIN", "medium", None, op_id or "?", code or None,
                      "operator ID ya PIN galat")
            tx_commit(conn)
            return err("Operator ID ya PIN galat hai", 401)

        row = get_by_code(conn, code)
        if row is None:
            log_event(conn, "UNKNOWN_CODE", "medium", op["lot_id"], op_id, code, "aisa booking code exist nahi karta")
            tx_commit(conn)
            return err("Ye booking code galat hai")

        if row["lot_id"] != op["lot_id"]:
            log_event(conn, "WRONG_LOT_CHECKIN", "high", op["lot_id"], op_id, code,
                      f"booking lot {row['lot_id']} ki hai, operator lot {op['lot_id']} ka hai")
            tx_commit(conn)
            return err("Ye booking is lot ki nahi hai")

        if row["status"] == "LOCKED":
            tx_commit(conn)
            return err("Ye booking lock hai. Admin se contact karo.", 423)
        if row["status"] != "CONFIRMED":
            tx_commit(conn)
            return err(f"Booking ka status {row['status']} hai, check-in nahi ho sakta", 409)

        if not hmac.compare_digest(otp_for(code).encode(), otp.encode()):
            fails = row["otp_fails"] + 1
            if fails >= MAX_OTP_FAILS:
                conn.execute("UPDATE reservations SET otp_fails=?, status='LOCKED' WHERE id=?", (fails, row["id"]))
                log_event(conn, "OTP_LOCK", "high", row["lot_id"], op_id, code, f"{fails} galat OTP, booking lock")
                msg = "3 galat OTP: booking lock ho gayi. Admin ko batao."
            else:
                conn.execute("UPDATE reservations SET otp_fails=? WHERE id=?", (fails, row["id"]))
                log_event(conn, "BAD_OTP", "medium", row["lot_id"], op_id, code, f"galat OTP ({fails}/{MAX_OTP_FAILS})")
                msg = f"OTP galat hai ({fails}/{MAX_OTP_FAILS})"
            tx_commit(conn)
            return err(msg, 403)

        # ---- sab theek: reserved slots ko occupied me badlo ----
        lot = conn.execute("SELECT * FROM lots WHERE id=?", (row["lot_id"],)).fetchone()
        new_count = min(lot["capacity"], lot["current_count"] + row["slots"])
        now = now_str()
        conn.execute("UPDATE lots SET current_count=?, last_updated=? WHERE id=?", (new_count, now, lot["id"]))
        conn.execute("UPDATE reservations SET status='CHECKED_IN', checkin_at=?, checkin_by=? WHERE id=?",
                     (now, op_id, row["id"]))
        bays = bays_of(conn, row["id"])
        conn.execute("UPDATE res_slots SET active=0 WHERE reservation_id=?", (row["id"],))
        conn.execute("INSERT INTO logs (lot_id, action, operator, timestamp) VALUES (?,?,?,?)",
                     (row["lot_id"], "RES_CHECKIN", op_id, now))   # isko Undo nahi kar sakte
        tx_commit(conn)
    except Exception:
        tx_rollback(conn)
        raise
    finally:
        conn.close()
    return jsonify({"ok": True, "slots": row["slots"], "bays": bays,
                    "user_name": row["user_name"], "vehicle_no": row["vehicle_no"]})


# ---------------- ADMIN APIs ----------------
@bp.route("/api/admin/reservations")
def admin_list():
    conn = get_tx_conn()
    try:
        tx_begin(conn)
        expire_old(conn)
        rows = conn.execute("""SELECT r.*, l.name AS lot_name FROM reservations r
                               JOIN lots l ON l.id = r.lot_id ORDER BY r.id DESC LIMIT 100""").fetchall()
        items = []
        for r in rows:
            items.append({"code": r["code"], "lot_name": r["lot_name"], "user_name": r["user_name"],
                          "phone": r["phone"], "vehicle_no": r["vehicle_no"], "slots": r["slots"],
                          "amount": r["amount"], "status": r["status"], "expires_at": r["expires_at"],
                          "created_at": r["created_at"], "bays": bays_of(conn, r["id"])})
        s = conn.execute("""SELECT
            COALESCE(SUM(CASE WHEN paid_at IS NOT NULL THEN amount - refund_amount ELSE 0 END), 0) AS revenue,
            COALESCE(SUM(CASE WHEN status='CONFIRMED' THEN 1 ELSE 0 END), 0) AS confirmed,
            COALESCE(SUM(CASE WHEN status='PENDING_PAYMENT' THEN 1 ELSE 0 END), 0) AS pending,
            COALESCE(SUM(CASE WHEN status='CHECKED_IN' THEN 1 ELSE 0 END), 0) AS checked_in,
            COALESCE(SUM(CASE WHEN status='NO_SHOW' THEN 1 ELSE 0 END), 0) AS no_show
            FROM reservations""").fetchone()
        tx_commit(conn)
    except Exception:
        tx_rollback(conn)
        raise
    finally:
        conn.close()
    return jsonify({"summary": dict(s), "items": items})


@bp.route("/api/admin/reservations/<code>/cancel", methods=["POST"])
def admin_cancel(code):
    data = request.get_json(silent=True) or {}
    reason = (data.get("reason") or "").strip() or "admin decision"
    conn = get_tx_conn()
    try:
        tx_begin(conn)
        row = get_by_code(conn, code)
        if row is None or row["status"] not in ("PENDING_PAYMENT", "CONFIRMED", "LOCKED"):
            tx_commit(conn)
            return err("Ye booking cancel nahi ho sakti", 409)
        refund = row["amount"] if row["paid_at"] else 0      # admin cancel = poora refund
        conn.execute("UPDATE reservations SET status='CANCELLED', cancel_reason=?, refund_amount=? WHERE id=?",
                     ("admin: " + reason, refund, row["id"]))
        conn.execute("UPDATE res_slots SET active=0 WHERE reservation_id=?", (row["id"],))
        log_event(conn, "ADMIN_CANCEL", "info", row["lot_id"], "admin", code, reason)
        tx_commit(conn)
    except Exception:
        tx_rollback(conn)
        raise
    finally:
        conn.close()
    return jsonify({"ok": True})


@bp.route("/api/admin/alerts")
def admin_alerts():
    conn = get_tx_conn()
    try:
        tx_begin(conn)
        alerts = []
        for e in conn.execute("SELECT * FROM security_events ORDER BY id DESC LIMIT 30"):
            alerts.append({"ts": e["ts"], "severity": e["severity"], "type": e["type"],
                           "operator": e["operator"], "lot_id": e["lot_id"],
                           "code": e["reservation_code"], "detail": e["detail"]})
        since = (datetime.now() - timedelta(hours=24)).strftime(FMT)
        rows = conn.execute(
            "SELECT operator, action, timestamp FROM logs WHERE timestamp >= ? ORDER BY id", (since,)).fetchall()
        tx_commit(conn)
    except Exception:
        tx_rollback(conn)
        raise
    finally:
        conn.close()

    # Pattern-based alerts (operator ke behaviour par)
    by_op = {}
    for r in rows:
        by_op.setdefault(r["operator"] or "?", []).append(r)
    for op, items in by_op.items():
        total = len(items)
        undos = sum(1 for i in items if i["action"] == "UNDO")
        if total >= 6 and undos / total > 0.25:
            alerts.append({"ts": items[-1]["timestamp"], "severity": "medium", "type": "UNDO_ABUSE",
                           "operator": op, "lot_id": None, "code": None,
                           "detail": f"{undos} undo / {total} actions (last 24h)"})
        times = [datetime.strptime(i["timestamp"], FMT) for i in items if i["action"] == "ENTRY"]
        for i in range(len(times) - 4):
            if (times[i + 4] - times[i]).total_seconds() <= 60:
                alerts.append({"ts": times[i + 4].strftime(FMT), "severity": "medium", "type": "BURST_ENTRY",
                               "operator": op, "lot_id": None, "code": None,
                               "detail": "5 ya zyada entries 60 second ke andar (bina verify kiye?)"})
                break

    alerts.sort(key=lambda a: a["ts"], reverse=True)
    return jsonify(alerts[:50])