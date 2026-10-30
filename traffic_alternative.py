"""
Smart Parking Alternative Suggestion (traffic-aware) - TomTom version.

Driver ek lot select karta hai -> yeh module TomTom Matrix Routing v2 (live traffic)
se driver ke origin se saare lots tak ka real road time nikalta hai
aur agar koi suitable lot kam se kam MIN_TIME_SAVING_MINUTES jaldi pahunchata hai
to use suggest karta hai.

Kuch bhi automatic change/reserve nahi hota. Traffic API fail ho to sirf
{"status": "unavailable"} jaata hai - koi nakli data nahi.
"""
import os
import threading
import time
from datetime import datetime
from math import radians, sin, cos, asin, sqrt

import requests
from dotenv import load_dotenv
from flask import Blueprint, jsonify, request

from database import (get_tx_conn, tx_begin, tx_commit, tx_rollback,
                      expire_old, reserved_counts)

try:
    from predict import predict_occupancy
except Exception:  # app.py jaisa hi fallback
    def predict_occupancy(lot_id, hour):
        return 50

load_dotenv()  # .env file se settings padho

bp = Blueprint("traffic_alternative", __name__)

ROUTES_URL = "https://api.tomtom.com/routing/matrix/2"


# ---------- settings (.env se, defaults ke saath) ----------
def _num(name, default):
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return float(default)


def settings():
    return {
        "api_key": os.environ.get("TOMTOM_API_KEY", "").strip(),
        "min_saving_s": _num("MIN_TIME_SAVING_MINUTES", 3) * 60,   # kam se kam itna time bachna chahiye
        "cache_s": _num("ROUTE_CACHE_SECONDS", 180),               # 2-5 minute cache
        "timeout_s": _num("ROUTES_TIMEOUT_SECONDS", 6),
        "min_free": int(_num("ALT_MIN_FREE_SPACES", 3)),           # isse kam jagah = unsuitable
        "max_pred": _num("ALT_MAX_PREDICTED_OCCUPANCY", 95),       # isse zyada predicted full = unsuitable
        "max_extra_walk_m": _num("ALT_MAX_EXTRA_WALK_M", 1000),    # destination se zyada door = unsuitable
    }


# ---------- helpers ----------
def distance_m(lat1, lng1, lat2, lng2):
    """app.py wale distance_m jaisa hi (circular import se bachne ke liye yahan copy)."""
    R = 6371000
    p1, p2 = radians(lat1), radians(lat2)
    dp = p2 - p1
    dl = radians(lng2 - lng1)
    a = sin(dp / 2) ** 2 + cos(p1) * cos(p2) * sin(dl / 2) ** 2
    return 2 * R * asin(sqrt(a))


def _coord(obj, key_lat="lat", key_lng="lng"):
    """{"lat":..,"lng":..} ko valid (lat, lng) floats me badlo, galat ho to None."""
    try:
        lat, lng = float(obj[key_lat]), float(obj[key_lng])
    except (TypeError, ValueError, KeyError):
        return None
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return None
    return lat, lng


# ---------- TomTom Matrix Routing API + cache ----------
_cache = {}
_cache_lock = threading.Lock()
_CACHE_MAX = 200


def _point(lat, lng):
    return {"point": {"latitude": lat, "longitude": lng}}


def fetch_route_matrix(origin, lots, cfg):
    """
    Ek hi API call: 1 origin -> saare lots. Return: {lot_id: {traffic_s, static_s, distance_m}}
    Koi bhi problem (timeout, HTTP error, galat JSON) ho to exception uthti hai.
    """
    body = {
        "origins": [_point(*origin)],
        "destinations": [_point(l["lat"], l["lng"]) for l in lots],
        "options": {
            "travelMode": "car",
            "routeType": "fastest",
            "departAt": "now",
            "traffic": "live",
        },
    }
    resp = requests.post(ROUTES_URL, params={"key": cfg["api_key"]}, json=body,
                         timeout=(3, cfg["timeout_s"]))
    try:
        resp.raise_for_status()
    except requests.exceptions.HTTPError as exc:
        # TomTom ka asli error (galat key, quota khatam...) terminal me dikhe
        raise RuntimeError(f"{exc} | TomTom says: {str(resp.text)[:300]}") from None
    data = resp.json()
    cells = data.get("data") if isinstance(data, dict) else None
    if not isinstance(cells, list):
        raise ValueError("unexpected TomTom response")

    routes = {}
    for el in cells:
        # sirf wahi elements lo jinka route sach me mila (routeSummary hai, error nahi)
        summary = el.get("routeSummary")
        if el.get("detailedError") or not summary:
            continue
        idx = el.get("destinationIndex")
        if idx is None or not (0 <= idx < len(lots)):
            continue
        traffic_s = summary.get("travelTimeInSeconds")
        distance = summary.get("lengthInMeters")
        if traffic_s is None or distance is None:
            continue
        delay_s = summary.get("trafficDelayInSeconds")
        static_s = None
        if delay_s is not None:
            static_s = max(0.0, float(traffic_s) - float(delay_s))  # bina traffic ka time
        routes[lots[idx]["id"]] = {
            "traffic_s": float(traffic_s),
            "static_s": static_s,   # None ho sakta hai - nakli value nahi bharte
            "distance_m": int(distance),
        }
    return routes


def get_routes(origin, lots, cfg):
    """Cache ke saath routes do. Return (routes, from_cache). Fail hone par cache nahi hota."""
    key = (round(origin[0], 4), round(origin[1], 4),
           tuple(sorted((l["id"], round(l["lat"], 5), round(l["lng"], 5)) for l in lots)))
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and hit[0] > now:
            return hit[1], True

    routes = fetch_route_matrix(origin, lots, cfg)

    with _cache_lock:
        if len(_cache) >= _CACHE_MAX:
            for k in [k for k, v in _cache.items() if v[0] <= now]:
                _cache.pop(k, None)
            if len(_cache) >= _CACHE_MAX:
                _cache.clear()
        _cache[key] = (now + cfg["cache_s"], routes)
    return routes, False


def clear_cache():
    with _cache_lock:
        _cache.clear()


# ---------- comparison ----------
def traffic_level(delay_s, static_s):
    """Delay ko 3 level me baanto: clear / moderate / heavy. Data na ho to None."""
    if delay_s is None or not static_s:
        return None
    ratio = delay_s / static_s
    if ratio < 0.10:
        return "clear"
    if ratio < 0.30:
        return "moderate"
    return "heavy"


def _mins(seconds):
    return None if seconds is None else int(round(seconds / 60))


def _lot_view(lot, route):
    delay_s = None
    if route["static_s"] is not None:
        delay_s = max(0.0, route["traffic_s"] - route["static_s"])
    return {
        "id": lot["id"],
        "name": lot["name"],
        "free": lot["free"],
        "predicted_occupancy": lot["pred"],
        "traffic_minutes": _mins(route["traffic_s"]),
        "static_minutes": _mins(route["static_s"]),
        "delay_minutes": _mins(delay_s),
        "traffic_level": traffic_level(delay_s, route["static_s"]),
        "road_distance_m": route["distance_m"],
        "distance_to_destination_m": round(lot["dist_dest"]),
    }


def pick_alternative(selected, candidates, routes, cfg):
    """
    selected: lot dict, candidates: baaki lots. Best suitable alternative ya None.
    Suitable = route mila, jagah kaafi, predicted occupancy theek, destination se zyada door nahi,
    aur kam se kam min_saving_s jaldi.
    """
    sel_route = routes[selected["id"]]
    best, best_score = None, None
    for lot in candidates:
        route = routes.get(lot["id"])
        if route is None:
            continue
        if lot["free"] < cfg["min_free"] or lot["pred"] > cfg["max_pred"]:
            continue
        if lot["dist_dest"] - selected["dist_dest"] > cfg["max_extra_walk_m"]:
            continue
        saved_s = sel_route["traffic_s"] - route["traffic_s"]
        if saved_s < cfg["min_saving_s"]:
            continue

        # zyada time bachat + zyada free jagah + kam predicted occupancy = behtar
        time_part = min(1.0, saved_s / sel_route["traffic_s"]) if sel_route["traffic_s"] else 0
        free_part = lot["free"] / lot["capacity"] if lot["capacity"] else 0
        score = 0.5 * time_part + 0.3 * free_part + 0.2 * (1 - lot["pred"] / 100)
        if best_score is None or score > best_score:
            best, best_score = (lot, route, saved_s), score
    return best


# ---------- API ----------
def _load_lots(hour):
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

    lots = []
    for r in rows:
        if r["lat"] is None or r["lng"] is None:
            continue
        free = max(0, r["capacity"] - r["current_count"] - res.get(r["id"], 0))
        lots.append({"id": r["id"], "name": r["name"], "capacity": r["capacity"],
                     "free": free, "lat": r["lat"], "lng": r["lng"],
                     "pred": predict_occupancy(r["id"], hour)})
    return lots


def _reply(status, **extra):
    return jsonify({"status": status, **extra})


@bp.route("/api/suggest-alternative", methods=["POST"])
def suggest_alternative():
    """
    Body: {"lot_id": 1, "origin": {"lat":..,"lng":..}, "destination": {"lat":..,"lng":..}, "hour": 12 (optional)}
    status: "found" (popup dikhao) | "none" (koi behtar nahi) | "unavailable" (traffic data nahi mila)
    """
    data = request.get_json(silent=True) or {}
    origin = _coord(data.get("origin") or {})
    dest = _coord(data.get("destination") or {})
    try:
        lot_id = int(data.get("lot_id"))
    except (TypeError, ValueError):
        lot_id = None
    if origin is None or dest is None or lot_id is None:
        return jsonify({"error": "lot_id, origin{lat,lng} aur destination{lat,lng} bhejo"}), 400

    hour = data.get("hour")
    try:
        hour = int(hour) if hour not in (None, "") else datetime.now().hour
    except (TypeError, ValueError):
        return jsonify({"error": "hour galat hai"}), 400

    cfg = settings()
    lots = _load_lots(hour)
    for l in lots:
        l["dist_dest"] = distance_m(dest[0], dest[1], l["lat"], l["lng"])

    selected = next((l for l in lots if l["id"] == lot_id), None)
    if selected is None:
        return jsonify({"error": "lot nahi mila"}), 404

    # API key nahi hai -> traffic nahi, normal selection chalega
    if not cfg["api_key"]:
        print("[suggest-alternative] TOMTOM_API_KEY nahi mili (.env check karo, server restart karo)")
        return _reply("unavailable", reason="no_api_key")

    try:
        routes, from_cache = get_routes(origin, lots, cfg)
    except Exception as exc:  # timeout, HTTP error, galat JSON... sab same tarah handle
        print(f"[suggest-alternative] TomTom API failed: {exc.__class__.__name__}: {exc}")
        return _reply("unavailable", reason="routes_api_failed")

    if selected["id"] not in routes:
        return _reply("unavailable", reason="no_route_for_selected_lot")

    others = [l for l in lots if l["id"] != selected["id"]]
    best = pick_alternative(selected, others, routes, cfg)
    if best is None:
        return _reply("none", cached=from_cache)

    alt, alt_route, saved_s = best
    return _reply(
        "found",
        cached=from_cache,
        threshold_minutes=cfg["min_saving_s"] / 60,
        saved_minutes=_mins(saved_s),
        extra_walk_m=round(alt["dist_dest"] - selected["dist_dest"]),
        selected=_lot_view(selected, routes[selected["id"]]),
        alternative=_lot_view(alt, alt_route),
    )