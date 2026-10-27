from flask import Flask, jsonify, request, render_template
from datetime import datetime
from math import radians, sin, cos, asin, sqrt
from database import get_conn, init_db

# Manjusha ka prediction function. Jab tak uski file ready nahi, 50 return hoga.
try:
    from predict import predict_occupancy
except Exception:
    def predict_occupancy(lot_id, hour):
        return 50

app = Flask(__name__)
init_db()


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def distance_m(lat1, lng1, lat2, lng2):
    """Do jagah ke beech ki doori meter me (haversine formula)"""
    R = 6371000
    p1, p2 = radians(lat1), radians(lat2)
    dp = p2 - p1
    dl = radians(lng2 - lng1)
    a = sin(dp / 2) ** 2 + cos(p1) * cos(p2) * sin(dl / 2) ** 2
    return 2 * R * asin(sqrt(a))


# ---------- PAGES ----------
@app.route("/")
def home():
    return "<h2>ParkAI</h2><a href='/operator'>Operator</a> | <a href='/user'>User</a> | <a href='/admin'>Admin</a>"


@app.route("/operator")
def operator_page():
    return render_template("operator.html")


@app.route("/user")
def user_page():
    return render_template("user.html")


@app.route("/admin")
def admin_page():
    return render_template("admin.html")


# ---------- API 1: lots dekhna + naya lot add ----------
@app.route("/api/lots", methods=["GET", "POST"])
def lots():
    conn = get_conn()

    if request.method == "POST":
        data = request.get_json()
        name = data.get("name")
        capacity = data.get("capacity")
        if not name or not capacity:
            conn.close()
            return jsonify({"error": "name aur capacity zaroori hai"}), 400
        conn.execute(
            "INSERT INTO lots (name, capacity, current_count, lat, lng, last_updated) VALUES (?,?,?,?,?,?)",
            (name, int(capacity), 0, data.get("lat"), data.get("lng"), now_str()),
        )
        conn.commit()
        conn.close()
        return jsonify({"ok": True})

    rows = conn.execute("SELECT * FROM lots").fetchall()
    conn.close()
    result = []
    for r in rows:
        lot = dict(r)
        lot["free"] = lot["capacity"] - lot["current_count"]
        result.append(lot)
    return jsonify(result)


# ---------- API 2: entry / exit ----------
@app.route("/api/update", methods=["POST"])
def update():
    data = request.get_json()
    lot_id = data.get("lot_id")
    action = data.get("action")
    operator = data.get("operator", "Op-1")

    if action not in ("ENTRY", "EXIT"):
        return jsonify({"error": "action ENTRY ya EXIT hona chahiye"}), 400

    conn = get_conn()
    lot = conn.execute("SELECT * FROM lots WHERE id=?", (lot_id,)).fetchone()
    if lot is None:
        conn.close()
        return jsonify({"error": "lot nahi mila"}), 404

    count = lot["current_count"]
    if action == "ENTRY":
        if count >= lot["capacity"]:
            conn.close()
            return jsonify({"error": "Lot full hai"}), 400
        count += 1
    else:
        if count <= 0:
            conn.close()
            return jsonify({"error": "Lot pehle se khaali hai"}), 400
        count -= 1

    now = now_str()
    conn.execute("UPDATE lots SET current_count=?, last_updated=? WHERE id=?", (count, now, lot_id))
    conn.execute(
        "INSERT INTO logs (lot_id, action, operator, timestamp) VALUES (?,?,?,?)",
        (lot_id, action, operator, now),
    )
    conn.commit()
    conn.close()
    return jsonify({
        "ok": True,
        "current_count": count,
        "free": lot["capacity"] - count,
        "last_updated": now,
    })


# ---------- API 3: undo ----------
@app.route("/api/undo", methods=["POST"])
def undo():
    data = request.get_json()
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
    conn.execute(
        "INSERT INTO logs (lot_id, action, operator, timestamp, undone) VALUES (?,?,?,?,1)",
        (lot_id, "UNDO", log["operator"], now),
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "current_count": count, "free": lot["capacity"] - count, "last_updated": now})


# ---------- API 4: activity logs ----------
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


# ---------- API 5: hourly history (graph ke liye) ----------
@app.route("/api/history/<int:lot_id>")
def history(lot_id):
    conn = get_conn()
    rows = conn.execute("""
        SELECT hour, ROUND(AVG(occupancy_percent)) AS occupancy
        FROM history WHERE lot_id=? GROUP BY hour ORDER BY hour
    """, (lot_id,)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


# ---------- API 6: recommendation (ranking) ----------
@app.route("/api/recommend")
def recommend():
    try:
        lat = float(request.args.get("lat"))
        lng = float(request.args.get("lng"))
    except (TypeError, ValueError):
        return jsonify({"error": "lat aur lng bhejo"}), 400

    # demo ke liye ?hour=12 bhej sakte ho. Nahi bheja toh abhi ka time
    hour_arg = request.args.get("hour")
    hour = int(hour_arg) if hour_arg else datetime.now().hour

    conn = get_conn()
    rows = conn.execute("SELECT * FROM lots").fetchall()
    conn.close()

    results = []
    for r in rows:
        free = r["capacity"] - r["current_count"]
        free_pct = free / r["capacity"]
        pred = predict_occupancy(r["id"], hour)          # 0 se 100
        dist = distance_m(lat, lng, r["lat"], r["lng"])  # meter
        dist_score = max(0, 1 - dist / 2000)             # 2 km se door = 0

        score = 0.4 * free_pct + 0.35 * (1 - pred / 100) + 0.25 * dist_score

        results.append({
            "id": r["id"],
            "name": r["name"],
            "free": free,
            "capacity": r["capacity"],
            "predicted_occupancy": pred,
            "distance_m": round(dist),
            "score": round(score, 3),
            "lat": r["lat"],
            "lng": r["lng"],
            "best": False,
        })

    results.sort(key=lambda x: x["score"], reverse=True)
    if results:
        results[0]["best"] = True
    return jsonify(results)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)