
from flask import Flask, jsonify, request, render_template
from datetime import datetime
from math import radians, sin, cos, asin, sqrt
from database import get_conn, init_db

# Prediction function
try:
    from predict import predict_occupancy
except ImportError:
    def predict_occupancy(lot_id, hour):
        return 50


app = Flask(__name__)

init_db()


# ---------- HELPER FUNCTIONS ----------

def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def distance_m(lat1, lng1, lat2, lng2):
    """Calculate distance between two locations in meters."""

    R = 6371000

    p1, p2 = radians(lat1), radians(lat2)
    dp = p2 - p1
    dl = radians(lng2 - lng1)

    a = (
        sin(dp / 2) ** 2
        + cos(p1) * cos(p2) * sin(dl / 2) ** 2
    )

    a = max(0, min(1, a))

    return 2 * R * asin(sqrt(a))


def valid_coordinates(lat, lng):
    return (
        -90 <= lat <= 90
        and -180 <= lng <= 180
    )


def get_prediction(lot_id, hour):
    """Safely obtain predicted occupancy."""

    try:
        prediction = float(predict_occupancy(lot_id, hour))

        if not 0 <= prediction <= 100:
            return 50

        return prediction

    except (ValueError, TypeError, Exception):
        return 50


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


# ---------- API 1: VIEW AND ADD PARKING LOTS ----------

@app.route("/api/lots", methods=["GET", "POST"])
def lots():

    conn = get_conn()

    try:

        if request.method == "POST":

            data = request.get_json(silent=True)

            if not isinstance(data, dict):
                return jsonify({
                    "error": "Valid JSON data required"
                }), 400

            name = data.get("name")
            capacity = data.get("capacity")

            try:
                capacity = int(capacity)
            except (ValueError, TypeError):
                return jsonify({
                    "error": "Capacity must be a valid integer"
                }), 400

            if not isinstance(name, str) or not name.strip():
                return jsonify({
                    "error": "Valid parking lot name required"
                }), 400

            if capacity <= 0:
                return jsonify({
                    "error": "Capacity must be greater than zero"
                }), 400

            lat = data.get("lat")
            lng = data.get("lng")

            if (lat is None) != (lng is None):
                return jsonify({
                    "error": "Both latitude and longitude are required"
                }), 400

            if lat is not None:

                try:
                    lat = float(lat)
                    lng = float(lng)
                except (ValueError, TypeError):
                    return jsonify({
                        "error": "Invalid coordinates"
                    }), 400

                if not valid_coordinates(lat, lng):
                    return jsonify({
                        "error": "Coordinates out of range"
                    }), 400

            conn.execute(
                """
                INSERT INTO lots
                (name, capacity, current_count, lat, lng, last_updated)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    name.strip(),
                    capacity,
                    0,
                    lat,
                    lng,
                    now_str()
                )
            )

            conn.commit()

            return jsonify({"ok": True}), 201

        rows = conn.execute(
            "SELECT * FROM lots"
        ).fetchall()

        result = []

        for r in rows:

            lot = dict(r)

            lot["free"] = (
                lot["capacity"] - lot["current_count"]
            )

            result.append(lot)

        return jsonify(result)

    finally:
        conn.close()


# ---------- API 2: VEHICLE ENTRY AND EXIT ----------

@app.route("/api/update", methods=["POST"])
def update():

    data = request.get_json(silent=True)

    if not isinstance(data, dict):
        return jsonify({
            "error": "Valid JSON data required"
        }), 400

    lot_id = data.get("lot_id")
    action = data.get("action")
    operator = data.get("operator", "Op-1")

    try:
        lot_id = int(lot_id)
    except (ValueError, TypeError):
        return jsonify({
            "error": "Valid lot_id required"
        }), 400

    if action not in ("ENTRY", "EXIT"):
        return jsonify({
            "error": "action must be ENTRY or EXIT"
        }), 400

    if not isinstance(operator, str) or not operator.strip():
        return jsonify({
            "error": "Valid operator required"
        }), 400

    conn = get_conn()

    try:

        # Start transaction
        conn.execute("BEGIN IMMEDIATE")

        lot = conn.execute(
            "SELECT * FROM lots WHERE id = ?",
            (lot_id,)
        ).fetchone()

        if lot is None:
            return jsonify({
                "error": "Parking lot not found"
            }), 404

        count = lot["current_count"]

        if action == "ENTRY":

            if count >= lot["capacity"]:
                return jsonify({
                    "error": "Lot is full"
                }), 400

            count += 1

        else:

            if count <= 0:
                return jsonify({
                    "error": "Parking lot is already empty"
                }), 400

            count -= 1

        now = now_str()

        conn.execute(
            """
            UPDATE lots
            SET current_count = ?, last_updated = ?
            WHERE id = ?
            """,
            (count, now, lot_id)
        )

        conn.execute(
            """
            INSERT INTO logs
            (lot_id, action, operator, timestamp, undone)
            VALUES (?, ?, ?, ?, 0)
            """,
            (lot_id, action, operator, now)
        )

        conn.commit()

        return jsonify({
            "ok": True,
            "current_count": count,
            "free": lot["capacity"] - count,
            "last_updated": now
        })

    except Exception:
        conn.rollback()
        app.logger.exception("Vehicle update failed")

        return jsonify({
            "error": "Unable to update parking record"
        }), 500

    finally:
        conn.close()


# ---------- API 3: UNDO LAST ENTRY OR EXIT ----------

@app.route("/api/undo", methods=["POST"])
def undo():

    data = request.get_json(silent=True)

    if not isinstance(data, dict):
        return jsonify({
            "error": "Valid JSON data required"
        }), 400

    try:
        lot_id = int(data.get("lot_id"))
    except (ValueError, TypeError):
        return jsonify({
            "error": "Valid lot_id required"
        }), 400

    conn = get_conn()

    try:

        conn.execute("BEGIN IMMEDIATE")

        # Undo only the latest activity.
        log = conn.execute(
            """
            SELECT * FROM logs
            WHERE lot_id = ?
              AND action IN ('ENTRY', 'EXIT')
              AND undone = 0
            ORDER BY id DESC
            LIMIT 1
            """,
            (lot_id,)
        ).fetchone()

        if log is None:
            return jsonify({
                "error": "Nothing to undo"
            }), 400

        lot = conn.execute(
            "SELECT * FROM lots WHERE id = ?",
            (lot_id,)
        ).fetchone()

        if lot is None:
            return jsonify({
                "error": "Parking lot not found"
            }), 404

        change = -1 if log["action"] == "ENTRY" else 1

        count = lot["current_count"] + change

        if not 0 <= count <= lot["capacity"]:
            return jsonify({
                "error": "Undo operation would create an invalid count"
            }), 400

        now = now_str()

        conn.execute(
            """
            UPDATE lots
            SET current_count = ?, last_updated = ?
            WHERE id = ?
            """,
            (count, now, lot_id)
        )

        conn.execute(
            "UPDATE logs SET undone = 1 WHERE id = ?",
            (log["id"],)
        )

        conn.execute(
            """
            INSERT INTO logs
            (lot_id, action, operator, timestamp, undone)
            VALUES (?, 'UNDO', ?, ?, 1)
            """,
            (lot_id, log["operator"], now)
        )

        conn.commit()

        return jsonify({
            "ok": True,
            "current_count": count,
            "free": lot["capacity"] - count,
            "last_updated": now
        })

    except Exception:
        conn.rollback()
        app.logger.exception("Undo operation failed")

        return jsonify({
            "error": "Unable to undo operation"
        }), 500

    finally:
        conn.close()


# ---------- API 4: ACTIVITY LOGS ----------

@app.route("/api/logs")
def logs():

    conn = get_conn()

    try:

        rows = conn.execute(
            """
            SELECT logs.id,
                   lots.name AS lot_name,
                   logs.action,
                   logs.operator,
                   logs.timestamp
            FROM logs
            JOIN lots ON logs.lot_id = lots.id
            ORDER BY logs.id DESC
            LIMIT 50
            """
        ).fetchall()

        return jsonify([
            dict(r) for r in rows
        ])

    finally:
        conn.close()


# ---------- API 5: HOURLY PARKING HISTORY ----------

@app.route("/api/history/<int:lot_id>")
def history(lot_id):

    conn = get_conn()

    try:

        rows = conn.execute(
            """
            SELECT hour,
                   ROUND(AVG(occupancy_percent)) AS occupancy
            FROM history
            WHERE lot_id = ?
            GROUP BY hour
            ORDER BY hour
            """,
            (lot_id,)
        ).fetchall()

        return jsonify([
            dict(r) for r in rows
        ])

    finally:
        conn.close()


# ---------- API 6: PARKING RECOMMENDATIONS ----------

@app.route("/api/recommend")
def recommend():

    try:

        lat = float(request.args.get("lat"))
        lng = float(request.args.get("lng"))

        if not valid_coordinates(lat, lng):
            raise ValueError

    except (ValueError, TypeError):
        return jsonify({
            "error": "Valid latitude and longitude required"
        }), 400

    hour_arg = request.args.get("hour")

    try:

        hour = (
            int(hour_arg)
            if hour_arg is not None
            else datetime.now().hour
        )

        if not 0 <= hour <= 23:
            raise ValueError

    except (ValueError, TypeError):
        return jsonify({
            "error": "Hour must be between 0 and 23"
        }), 400

    conn = get_conn()

    try:

        rows = conn.execute(
            "SELECT * FROM lots"
        ).fetchall()

    finally:
        conn.close()

    results = []

    for r in rows:

        if r["lat"] is None or r["lng"] is None:
            continue

        capacity = r["capacity"]
        current_count = r["current_count"]

        if capacity <= 0:
            continue

        if not valid_coordinates(r["lat"], r["lng"]):
            continue

        free = capacity - current_count

        if not 0 <= free <= capacity:
            continue

        free_pct = free / capacity

        pred = get_prediction(r["id"], hour)

        dist = distance_m(
            lat,
            lng,
            r["lat"],
            r["lng"]
        )

        dist_score = max(
            0,
            1 - dist / 2000
        )

        score = (
            0.40 * free_pct
            + 0.35 * (1 - pred / 100)
            + 0.25 * dist_score
        )

        results.append({

            "id": r["id"],
            "name": r["name"],
            "free": free,
            "capacity": capacity,
            "predicted_occupancy": round(pred, 2),
            "distance_m": round(dist),
            "score": round(score, 3),
            "lat": r["lat"],
            "lng": r["lng"],
            "best": False

        })

    results.sort(
        key=lambda x: x["score"],
        reverse=True
    )

    if results:
        results[0]["best"] = True

    return jsonify(results)


# ---------- RUN APPLICATION ----------

if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=5000,
        debug=True
    )
