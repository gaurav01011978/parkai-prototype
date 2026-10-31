from flask import Flask, jsonify, request, render_template
from datetime import datetime
from math import radians, sin, cos, asin, sqrt

from database import (get_conn, get_tx_conn, tx_begin, tx_commit, tx_rollback, init_db,
                      expire_old, reserved_counts, log_event, now_str)
from reservations import bp as reservations_bp
from traffic_alternative import bp as traffic_bp
from advance import bp as advance_bp, init_advance_db   # NEW: advance reservation + early arrival + pricing
from subscriptions import bp as subscriptions_bp, init_subscription_db   # NEW: subscription plans + payments
from assistant import bp as assistant_bp, init_assistant_db              # NEW: AI parking assistant

try:
    from predict import predict_occupancy
except Exception:
    def predict_occupancy(lot_id, hour):
        return 50

app = Flask(__name__)
init_db()
init_advance_db()   # NEW: safe migration (sirf naye tables banata hai)
init_subscription_db()   # NEW: safe migration
init_assistant_db()      # NEW: safe migration
app.register_blueprint(reservations_bp)
app.register_blueprint(traffic_bp)
app.register_blueprint(advance_bp)   # NEW
app.register_blueprint(subscriptions_bp)   # NEW
app.register_blueprint(assistant_bp)       # NEW


def distance_m(lat1, lng1, lat2, lng2):
    R = 6371000
    p1, p2 = radians(lat1), radians(lat2)
    dp = p2 - p1
    dl = radians(lng2 - lng1)
    a = sin(dp / 2) ** 2 + cos(p1) * cos(p2) * sin(dl / 2) ** 2
    return 2 * R * asin(sqrt(a))


# ---------- PAGES ----------
@app.route("/")
def home():
    return render_template("index.html")


@app.route("/operator")
def operator_page():
    return render_template("operator.html")


@app.route("/user")
def user_page():
    return render_template("user.html")


@app.route("/admin")
def admin_page():
    return render_template("admin.html")


@app.route("/reserve")
def reserve_page():
    return render_template("reserve.html")


@app.route("/pay/<code>")
def pay_page(code):
    return render_template("pay.html")


@app.route("/ticket/<code>")
def ticket_page(code):
    return render_template("ticket.html")


@app.route("/checkin")
def checkin_page():
    return render_template("checkin.html")


@app.route("/admin/security")
def security_page():
    return render_template("security.html")


# ---------- API: lots ----------
@app.route("/api/lots", methods=["GET", "POST"])
def lots():
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        name = data.get("name")
        capacity = data.get("capacity")
        if not name or not capacity:
            return jsonify({"error": "name aur capacity zaroori hai"}), 400
        conn = get_conn()
        conn.execute(
            "INSERT INTO lots (name, capacity, current_count, lat, lng, last_updated) VALUES (?,?,?,?,?,?)",
            (name, int(capacity), 0, data.get("lat"), data.get("lng"), now_str()),
        )
        conn.commit()
        conn.close()
        return jsonify({"ok": True})

    conn = get_tx_conn()
    try:
        tx_begin(conn)
        expire_old(conn)
        rows = conn.execute("SELECT * FROM lots").fetchall()
        res = reserved_counts(conn)
        tx_commit(conn)
    except Exception:
        tx_rollback(conn)
        raise
    finally:
        conn.close()

    result = []
    for r in rows:
        lot = dict(r)
        lot["reserved"] = res.get(lot["id"], 0)
        # free = walk-in aur naye booking ke liye bachi jagah (reserved hata ke)
        lot["free"] = max(0, lot["capacity"] - lot["current_count"] - lot["reserved"])
        result.append(lot)
    return jsonify(result)


# ---------- API: entry / exit ----------
@app.route("/api/update", methods=["POST"])
def update():
    data = request.get_json(silent=True) or {}
    lot_id = data.get("lot_id")
    action = data.get("action")
    operator = data.get("operator", "Op-1")
    if action not in ("ENTRY", "EXIT"):
        return jsonify({"error": "action ENTRY ya EXIT hona chahiye"}), 400

    conn = get_tx_conn()
    try:
        tx_begin(conn)
        expire_old(conn)
        lot = conn.execute("SELECT * FROM lots WHERE id=?", (lot_id,)).fetchone()
        if lot is None:
            tx_rollback(conn)
            return jsonify({"error": "lot nahi mila"}), 404
        reserved = reserved_counts(conn).get(lot["id"], 0)
        count = lot["current_count"]
        if action == "ENTRY":
            if count >= lot["capacity"]:
                tx_rollback(conn)
                return jsonify({"error": "Lot full hai"}), 400
            if count + reserved >= lot["capacity"]:
                # baaki jagah reserved hai: walk-in entry roko aur alert banao
                log_event(conn, "BLOCKED_ENTRY", "medium", lot["id"], operator, None,
                          f"walk-in entry roki gayi, {reserved} slots reserved hain")
                tx_commit(conn)
                return jsonify({"error": "Baaki jagah reserved hai. Reserved gaadi sirf Code + OTP se check-in hongi."}), 409
            count += 1
        else:
            if count <= 0:
                tx_rollback(conn)
                return jsonify({"error": "Lot pehle se khaali hai"}), 400
            count -= 1
        now = now_str()
        conn.execute("UPDATE lots SET current_count=?, last_updated=? WHERE id=?", (count, now, lot["id"]))
        conn.execute("INSERT INTO logs (lot_id, action, operator, timestamp) VALUES (?,?,?,?)",
                     (lot["id"], action, operator, now))
        tx_commit(conn)
    except Exception:
        tx_rollback(conn)
        raise
    finally:
        conn.close()

    return jsonify({"ok": True, "current_count": count, "reserved": reserved,
                    "free": max(0, lot["capacity"] - count - reserved), "last_updated": now})


# ---------- API: undo ----------
@app.route("/api/undo", methods=["POST"])
def undo():
    data = request.get_json(silent=True) or {}
    lot_id = data.get("lot_id")
    conn = get_conn()
    log = conn.execute(
        "SELECT * FROM logs WHERE lot_id=? AND action IN ('ENTRY','EXIT') AND undone=0 ORDER BY id DESC LIMIT 1",
        (lot_id,),
    ).fetchone()
    if log is None:
        conn.close()
        return jsonify({"error": "Undo karne ke liye kuch nahi hai"}), 400
    lot = conn.execute("SELECT * FROM lots WHERE id=?", (lot_id,)).fetchone()
    change = -1 if log["action"] == "ENTRY" else 1
    count = max(0, min(lot["capacity"], lot["current_count"] + change))
    now = now_str()
    conn.execute("UPDATE lots SET current_count=?, last_updated=? WHERE id=?", (count, now, lot_id))
    conn.execute("UPDATE logs SET undone=1 WHERE id=?", (log["id"],))
    conn.execute("INSERT INTO logs (lot_id, action, operator, timestamp, undone) VALUES (?,?,?,?,1)",
                 (lot_id, "UNDO", log["operator"], now))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "current_count": count, "last_updated": now})


# ---------- API: logs, history ----------
@app.route("/api/logs")
def logs():
    conn = get_conn()
    rows = conn.execute("""
        SELECT logs.id, lots.name AS lot_name, logs.action, logs.operator, logs.timestamp
        FROM logs JOIN lots ON logs.lot_id = lots.id
        ORDER BY logs.id DESC LIMIT 50
    """).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/history/<int:lot_id>")
def history(lot_id):
    conn = get_conn()
    rows = conn.execute("""
        SELECT hour, ROUND(AVG(occupancy_percent)) AS occupancy
        FROM history WHERE lot_id=? GROUP BY hour ORDER BY hour
    """, (lot_id,)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


# ---------- API: recommendation ----------
@app.route("/api/recommend")
def recommend():
    try:
        lat = float(request.args.get("lat"))
        lng = float(request.args.get("lng"))
    except (TypeError, ValueError):
        return jsonify({"error": "lat aur lng bhejo"}), 400
    hour_arg = request.args.get("hour")
    hour = int(hour_arg) if hour_arg else datetime.now().hour

    conn = get_tx_conn()
    try:
        tx_begin(conn)
        expire_old(conn)
        rows = conn.execute("SELECT * FROM lots").fetchall()
        res = reserved_counts(conn)
        tx_commit(conn)
    except Exception:
        tx_rollback(conn)
        raise
    finally:
        conn.close()

    results = []
    for r in rows:
        reserved = res.get(r["id"], 0)
        free = max(0, r["capacity"] - r["current_count"] - reserved)
        free_pct = free / r["capacity"]
        pred = predict_occupancy(r["id"], hour)
        dist = distance_m(lat, lng, r["lat"], r["lng"])
        dist_score = max(0, 1 - dist / 2000)
        score = 0.4 * free_pct + 0.35 * (1 - pred / 100) + 0.25 * dist_score
        results.append({
            "id": r["id"], "name": r["name"], "free": free, "reserved": reserved,
            "capacity": r["capacity"], "predicted_occupancy": pred,
            "distance_m": round(dist), "score": round(score, 3),
            "lat": r["lat"], "lng": r["lng"], "best": False,
        })
    results.sort(key=lambda x: x["score"], reverse=True)
    if results:
        results[0]["best"] = True
    return jsonify(results)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)