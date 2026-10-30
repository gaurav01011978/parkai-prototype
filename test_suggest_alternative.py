"""
Run:  python -m unittest test_suggest_alternative -v

Google ko asli call nahi jaata: sirf test me requests.post ko mock kiya gaya hai
(Google jaisa JSON dekar). App ke code me koi nakli traffic data nahi hai.
"""
import os
import tempfile
import unittest
from unittest.mock import patch, MagicMock

import requests

import database

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
database.DB_NAME = _tmp.name          # test ke liye alag database (parkai.db safe rahega)

import app as parkai_app              # noqa: E402  (DB_NAME set hone ke baad import)
import traffic_alternative as ta      # noqa: E402

# demo lots: id1 Lot A (26 free), id2 Lot B (8 free), id3 Lot C (40 free)
ORIGIN = {"lat": 28.5245, "lng": 77.2066}
DEST_MALL = {"lat": 28.5494, "lng": 77.2530}
DEST_MARKET = {"lat": 28.5510, "lng": 77.2560}


def element(dest_index, traffic_min, static_min=None, dist=3000, condition="ROUTE_EXISTS", status=None):
    el = {"originIndex": 0, "destinationIndex": dest_index, "condition": condition}
    if status:
        el["status"] = status
    if condition == "ROUTE_EXISTS":
        el["distanceMeters"] = dist
        el["duration"] = f"{int(traffic_min * 60)}s"
        if static_min is not None:
            el["staticDuration"] = f"{int(static_min * 60)}s"
    return el


def google_ok(*elements):
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = list(elements)
    return resp


class Base(unittest.TestCase):
    def setUp(self):
        os.environ["GOOGLE_MAPS_API_KEY"] = "test-key"
        os.environ.pop("MIN_TIME_SAVING_MINUTES", None)
        ta.clear_cache()
        conn = database.get_conn()
        for lot_id, cur in ((1, 74), (2, 52), (3, 110)):
            conn.execute("UPDATE lots SET current_count=? WHERE id=?", (cur, lot_id))
        conn.commit()
        conn.close()
        self.client = parkai_app.app.test_client()

    def ask(self, lot_id=1, origin=ORIGIN, dest=DEST_MALL, **extra):
        body = {"lot_id": lot_id, "origin": origin, "destination": dest, **extra}
        r = self.client.post("/api/suggest-alternative", json=body)
        return r.status_code, r.get_json()

    def set_count(self, lot_id, count):
        conn = database.get_conn()
        conn.execute("UPDATE lots SET current_count=? WHERE id=?", (count, lot_id))
        conn.commit()
        conn.close()


class Scenarios(Base):
    # 1. Lot A 12 min, Lot B 6 min -> popup
    def test_popup_when_alternative_saves_6_minutes(self):
        with patch("traffic_alternative.requests.post") as post:
            post.return_value = google_ok(element(0, 12, 9, 4200), element(1, 6, 5, 4600), element(2, 11, 9, 4300))
            code, d = self.ask(lot_id=1)
        self.assertEqual(code, 200)
        self.assertEqual(d["status"], "found")
        self.assertEqual(d["alternative"]["name"], "Lot B")
        self.assertEqual(d["saved_minutes"], 6)
        self.assertEqual(d["selected"]["traffic_minutes"], 12)
        self.assertEqual(d["selected"]["delay_minutes"], 3)           # 12 - 9
        self.assertEqual(d["selected"]["traffic_level"], "heavy")      # delay 3 min / static 9 min = 33%
        self.assertEqual(d["selected"]["road_distance_m"], 4200)
        self.assertEqual(d["alternative"]["road_distance_m"], 4600)
        self.assertIn("extra_walk_m", d)

    # 2. sirf 1 minute bachta hai -> popup nahi
    def test_no_popup_when_saving_only_1_minute(self):
        with patch("traffic_alternative.requests.post") as post:
            post.return_value = google_ok(element(0, 12, 10), element(1, 11, 10), element(2, 12, 10))
            _, d = self.ask(lot_id=1)
        self.assertEqual(d["status"], "none")
        self.assertNotIn("alternative", d)

    def test_exactly_threshold_counts_just_below_does_not(self):
        with patch("traffic_alternative.requests.post") as post:
            post.return_value = google_ok(element(0, 12, 10), element(1, 9, 8), element(2, 12, 10))
            _, d = self.ask(lot_id=1)
            self.assertEqual(d["status"], "found")            # exactly 3 min
            ta.clear_cache()
            post.return_value = google_ok(element(0, 12, 10), element(1, 9.1, 8), element(2, 12, 10))
            _, d = self.ask(lot_id=1)
            self.assertEqual(d["status"], "none")             # 2m54s < 3 min

    def test_threshold_is_configurable(self):
        os.environ["MIN_TIME_SAVING_MINUTES"] = "1"
        with patch("traffic_alternative.requests.post") as post:
            post.return_value = google_ok(element(0, 12, 10), element(1, 11, 10), element(2, 12, 10))
            _, d = self.ask(lot_id=1)
        self.assertEqual(d["status"], "found")

    # 3. alternative full -> exclude
    def test_full_alternative_is_excluded(self):
        self.set_count(2, 60)  # Lot B full
        self.set_count(3, 150)  # Lot C full
        with patch("traffic_alternative.requests.post") as post:
            post.return_value = google_ok(element(0, 12, 10), element(1, 6, 5), element(2, 5, 4))
            _, d = self.ask(lot_id=1)
        self.assertEqual(d["status"], "none")

    def test_full_alternative_skipped_but_next_suitable_one_picked(self):
        self.set_count(2, 60)  # Lot B full
        with patch("traffic_alternative.requests.post") as post:
            post.return_value = google_ok(element(0, 12, 10), element(1, 5, 4), element(2, 8, 7))
            _, d = self.ask(lot_id=1)
        self.assertEqual(d["status"], "found")
        self.assertEqual(d["alternative"]["name"], "Lot C")

    def test_reserved_slots_count_as_not_free(self):
        # Lot B me 8 free hai; 6 reserved ho gaye -> sirf 2 free (< 3) -> unsuitable
        conn = database.get_conn()
        conn.execute("""INSERT INTO reservations (code,token,lot_id,user_name,phone,slots,hold_minutes,amount,
                        status,created_at,pay_by,expires_at) VALUES ('PK-TEST01','t',2,'x','1',6,30,10,'CONFIRMED',
                        '2000-01-01 00:00:00','2999-01-01 00:00:00','2999-01-01 00:00:00')""")
        rid = conn.execute("SELECT id FROM reservations WHERE code='PK-TEST01'").fetchone()[0]
        for bay in range(1, 7):
            conn.execute("INSERT INTO res_slots (reservation_id, lot_id, bay_no, active) VALUES (?,?,?,1)", (rid, 2, bay))
        conn.commit()
        conn.close()
        try:
            with patch("traffic_alternative.requests.post") as post:
                post.return_value = google_ok(element(0, 12, 10), element(1, 5, 4), element(2, 12, 10))
                _, d = self.ask(lot_id=1)
            self.assertEqual(d["status"], "none")
        finally:
            conn = database.get_conn()
            conn.execute("DELETE FROM res_slots WHERE reservation_id=?", (rid,))
            conn.execute("DELETE FROM reservations WHERE id=?", (rid,))
            conn.commit()
            conn.close()

    # 4. koi suitable alternative nahi (selected already fastest)
    def test_selected_lot_already_fastest(self):
        with patch("traffic_alternative.requests.post") as post:
            post.return_value = google_ok(element(0, 5, 4), element(1, 9, 8), element(2, 12, 10))
            _, d = self.ask(lot_id=1)
        self.assertEqual(d["status"], "none")

    def test_alternative_too_far_from_destination_is_unsuitable(self):
        os.environ["ALT_MAX_EXTRA_WALK_M"] = "100"
        try:
            with patch("traffic_alternative.requests.post") as post:
                post.return_value = google_ok(element(0, 12, 10), element(1, 6, 5), element(2, 12, 10))
                # Lot B (id2) destination ke paas hai, Lot A se dur nahi -> extra walk negative -> allowed
                _, d = self.ask(lot_id=1)
                self.assertEqual(d["status"], "found")
                ta.clear_cache()
                post.return_value = google_ok(element(0, 12, 10), element(1, 12, 10), element(2, 5, 4))
                # Lot C Lot A se ~300 m door -> 100 m limit se zyada
                _, d = self.ask(lot_id=1)
                self.assertEqual(d["status"], "none")
        finally:
            os.environ.pop("ALT_MAX_EXTRA_WALK_M", None)

    # alternatives me preference: zyada free + kam predicted occupancy
    def test_prefers_more_free_spaces_when_times_are_close(self):
        with patch("traffic_alternative.requests.post") as post:
            post.return_value = google_ok(element(0, 12, 10), element(1, 6, 5), element(2, 6.5, 5))
            _, d = self.ask(lot_id=1)
        self.assertEqual(d["alternative"]["name"], "Lot C")   # 40 free vs 8 free

    def test_prefers_lower_predicted_occupancy(self):
        preds = {1: 50, 2: 90, 3: 30}
        with patch("traffic_alternative.predict_occupancy", side_effect=lambda lot_id, hour: preds[lot_id]), \
                patch("traffic_alternative.requests.post") as post:
            post.return_value = google_ok(element(0, 12, 10), element(1, 6, 5), element(2, 6, 5))
            _, d = self.ask(lot_id=1)
        self.assertEqual(d["alternative"]["name"], "Lot C")

    def test_very_crowded_predicted_lot_is_unsuitable(self):
        preds = {1: 50, 2: 99, 3: 99}
        with patch("traffic_alternative.predict_occupancy", side_effect=lambda lot_id, hour: preds[lot_id]), \
                patch("traffic_alternative.requests.post") as post:
            post.return_value = google_ok(element(0, 12, 10), element(1, 6, 5), element(2, 6, 5))
            _, d = self.ask(lot_id=1)
        self.assertEqual(d["status"], "none")


class ApiFailure(Base):
    def assert_unavailable(self, d):
        self.assertEqual(d["status"], "unavailable")
        for k in ("alternative", "selected", "saved_minutes"):
            self.assertNotIn(k, d)               # koi nakli traffic info nahi

    # 5. API failure -> normal selection chale (frontend ko "unavailable" milta hai, 200 OK)
    def test_timeout(self):
        with patch("traffic_alternative.requests.post", side_effect=requests.exceptions.Timeout()):
            code, d = self.ask()
        self.assertEqual(code, 200)
        self.assert_unavailable(d)

    def test_http_error_like_wrong_key_or_billing_off(self):
        resp = MagicMock()
        resp.raise_for_status.side_effect = requests.exceptions.HTTPError("403 Forbidden")
        with patch("traffic_alternative.requests.post", return_value=resp):
            _, d = self.ask()
        self.assert_unavailable(d)

    def test_invalid_json(self):
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.side_effect = ValueError("not json")
        with patch("traffic_alternative.requests.post", return_value=resp):
            _, d = self.ask()
        self.assert_unavailable(d)

    def test_error_object_instead_of_list(self):
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {"error": {"code": 400, "message": "bad"}}
        with patch("traffic_alternative.requests.post", return_value=resp):
            _, d = self.ask()
        self.assert_unavailable(d)

    def test_no_api_key(self):
        os.environ["GOOGLE_MAPS_API_KEY"] = ""
        with patch("traffic_alternative.requests.post") as post:
            _, d = self.ask()
        self.assert_unavailable(d)
        post.assert_not_called()

    def test_route_not_found_for_selected_lot(self):
        with patch("traffic_alternative.requests.post") as post:
            post.return_value = google_ok(element(0, 0, condition="ROUTE_NOT_FOUND"),
                                          element(1, 6, 5), element(2, 6, 5))
            _, d = self.ask(lot_id=1)
        self.assert_unavailable(d)

    def test_route_not_found_for_alternative_is_just_skipped(self):
        with patch("traffic_alternative.requests.post") as post:
            post.return_value = google_ok(element(0, 12, 10),
                                          element(1, 0, condition="ROUTE_NOT_FOUND"),
                                          element(2, 6, 5, status={"code": 3, "message": "bad"}))
            _, d = self.ask(lot_id=1)
        self.assertEqual(d["status"], "none")

    def test_failure_is_not_cached(self):
        with patch("traffic_alternative.requests.post", side_effect=requests.exceptions.Timeout()) as post:
            self.ask()
            self.ask()
            self.assertEqual(post.call_count, 2)   # dobara try hua
        with patch("traffic_alternative.requests.post") as post:
            post.return_value = google_ok(element(0, 12, 10), element(1, 6, 5), element(2, 12, 10))
            _, d = self.ask()
        self.assertEqual(d["status"], "found")

    def test_missing_static_duration_is_not_invented(self):
        with patch("traffic_alternative.requests.post") as post:
            post.return_value = google_ok(element(0, 12), element(1, 6), element(2, 12))
            _, d = self.ask(lot_id=1)
        self.assertEqual(d["status"], "found")
        self.assertIsNone(d["selected"]["delay_minutes"])
        self.assertIsNone(d["selected"]["traffic_level"])


class CacheAndRefresh(Base):
    def one_call(self, post, **kw):
        post.return_value = google_ok(element(0, 12, 10), element(1, 6, 5), element(2, 12, 10))
        return self.ask(**kw)[1]

    def test_cache_used_for_repeat_clicks(self):
        with patch("traffic_alternative.requests.post") as post:
            first = self.one_call(post)
            second = self.one_call(post)
            self.assertEqual(post.call_count, 1)
        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"])

    # 8. origin badle -> fresh call
    def test_origin_change_calls_google_again(self):
        with patch("traffic_alternative.requests.post") as post:
            self.one_call(post)
            self.one_call(post, origin={"lat": 28.6129, "lng": 77.2295})
            self.assertEqual(post.call_count, 2)

    def test_tiny_gps_jitter_still_uses_cache(self):
        with patch("traffic_alternative.requests.post") as post:
            self.one_call(post)
            self.one_call(post, origin={"lat": ORIGIN["lat"] + 0.00001, "lng": ORIGIN["lng"]})
            self.assertEqual(post.call_count, 1)

    def test_cache_expires(self):
        with patch("traffic_alternative.requests.post") as post, patch("traffic_alternative.time.time") as clock:
            clock.return_value = 1000.0
            self.one_call(post)
            clock.return_value = 1000.0 + 179
            self.one_call(post)
            self.assertEqual(post.call_count, 1)
            clock.return_value = 1000.0 + 181
            self.one_call(post)
            self.assertEqual(post.call_count, 2)

    # destination / selected lot badle -> comparison fresh (Google ka driving time cache se, comparison naya)
    def test_changing_destination_recomputes_comparison(self):
        with patch("traffic_alternative.requests.post") as post:
            a = self.one_call(post, dest=DEST_MALL)
            b = self.one_call(post, dest=DEST_MARKET)
        self.assertNotEqual(a["selected"]["distance_to_destination_m"], b["selected"]["distance_to_destination_m"])
        self.assertNotEqual(a["extra_walk_m"], b["extra_walk_m"])

    def test_changing_selected_lot_recomputes_comparison(self):
        with patch("traffic_alternative.requests.post") as post:
            a = self.one_call(post, lot_id=1)      # A 12 min -> B behtar
            b = self.one_call(post, lot_id=2)      # B 6 min already fastest -> koi alternative nahi
        self.assertEqual(a["status"], "found")
        self.assertEqual(b["status"], "none")

    def test_google_request_shape_and_key_stays_server_side(self):
        with patch("traffic_alternative.requests.post") as post:
            post.return_value = google_ok(element(0, 12, 10), element(1, 6, 5), element(2, 12, 10))
            code, d = self.ask()
        args, kwargs = post.call_args
        self.assertEqual(args[0], "https://routes.googleapis.com/distanceMatrix/v2:computeRouteMatrix")
        self.assertEqual(kwargs["json"]["routingPreference"], "TRAFFIC_AWARE")
        self.assertEqual(kwargs["json"]["travelMode"], "DRIVE")
        self.assertEqual(len(kwargs["json"]["origins"]), 1)
        self.assertEqual(len(kwargs["json"]["destinations"]), 3)
        self.assertEqual(kwargs["headers"]["X-Goog-Api-Key"], "test-key")
        self.assertIn("staticDuration", kwargs["headers"]["X-Goog-FieldMask"])
        self.assertIn("timeout", kwargs)
        self.assertNotIn("test-key", str(d))             # frontend ko key kabhi nahi jaati


class Validation(Base):
    def test_bad_input_is_400(self):
        for body in ({}, {"lot_id": 1}, {"lot_id": "x", "origin": ORIGIN, "destination": DEST_MALL},
                     {"lot_id": 1, "origin": {"lat": 999, "lng": 0}, "destination": DEST_MALL},
                     {"lot_id": 1, "origin": ORIGIN, "destination": {"lat": "a", "lng": "b"}}):
            r = self.client.post("/api/suggest-alternative", json=body)
            self.assertEqual(r.status_code, 400, body)

    def test_unknown_lot_is_404(self):
        with patch("traffic_alternative.requests.post") as post:
            code, _ = self.ask(lot_id=999)
        self.assertEqual(code, 404)
        post.assert_not_called()


class ExistingFeaturesStillWork(Base):
    def test_pages_and_old_apis(self):
        for url in ("/", "/user", "/operator", "/admin", "/reserve", "/checkin", "/admin/security"):
            self.assertEqual(self.client.get(url).status_code, 200, url)
        self.assertEqual(len(self.client.get("/api/lots").get_json()), 3)
        rec = self.client.get("/api/recommend?lat=28.5494&lng=77.2530").get_json()
        self.assertEqual(len(rec), 3)
        self.assertTrue(any(r["best"] for r in rec))
        self.assertEqual(self.client.get("/api/reservations/config").status_code, 200)


if __name__ == "__main__":
    unittest.main()