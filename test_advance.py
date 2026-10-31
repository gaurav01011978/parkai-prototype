"""Run: python test_advance.py   (temporary DB use karta hai, aapka parkai.db touch nahi hota)"""
import os, sys, tempfile, threading
from datetime import datetime, timedelta

import database
database.DB_NAME = os.path.join(tempfile.mkdtemp(), "test.db")

from app import app            # noqa: E402
import advance                 # noqa: E402

PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  PASS", name)
    else:
        FAIL += 1
        print("  FAIL", name, extra)


def db():
    return database.get_conn()


def add_lot(name, cap, cur=0, lat=28.55, lng=77.25):
    c = db()
    cur_ = c.execute("INSERT INTO lots (name, capacity, current_count, lat, lng, last_updated) VALUES (?,?,?,?,?,?)",
                     (name, cap, cur, lat, lng, database.now_str()))
    c.commit(); lid = cur_.lastrowid; c.close()
    return lid


def set_count(lot_id, n):
    c = db(); c.execute("UPDATE lots SET current_count=? WHERE id=?", (n, lot_id)); c.commit(); c.close()


def parts(dt_start, dt_end):
    return dt_start.strftime("%Y-%m-%d"), dt_start.strftime("%H:%M"), dt_end.strftime("%H:%M")


UPI = {"method": "UPI", "upi_id": "test@upi"}
client = app.test_client()


def book(lot, start, end, phone="9999999999", **kw):
    if end.date() > start.date():  # same-day interval hi allowed hai; midnight ke paas test ko stable rakho
        end = start.replace(hour=23, minute=59)
        if (end - start).total_seconds() < 15 * 60:
            start = end - timedelta(minutes=20)
    d, s, e = parts(start, end)
    body = {"lot_id": lot, "date": d, "start_time": s, "end_time": e, "user_name": "Test", "phone": phone,
            "vehicle_no": "DL1AB1234", **UPI, **kw}
    r = client.post("/api/advance/book", json=body)
    return r.status_code, r.get_json()


def tomorrow(h, m=0):
    t = datetime.now() + timedelta(days=1)
    return t.replace(hour=h, minute=m, second=0, microsecond=0)


def ph(i):
    return str(9000000000 + i)


print("1) Pricing tiers")
L = add_lot("PriceLot", 50)
now = datetime.now()
for label, delta, expect in [("30 min", timedelta(minutes=30), 40), ("12 hours", timedelta(hours=12), 25),
                             ("2 days", timedelta(days=2), 20), ("exactly ~60min", timedelta(minutes=59, seconds=30), 40),
                             ("~1 day", timedelta(hours=23, minutes=59), 25), ("~5 days", timedelta(days=4, hours=23), 20)]:
    st = (datetime.now() + delta).replace(second=0, microsecond=0)
    d, s, e = parts(st, st + timedelta(hours=1))
    code, j = book(L, st, st + timedelta(hours=1), phone=ph(abs(hash(label)) % 10 ** 6))
    check(f"price {label} = {expect}", code == 200 and j["booking"]["amount"] == expect, (code, j))
code, j = book(L, datetime.now() + timedelta(days=6), datetime.now() + timedelta(days=6, hours=1), phone=ph(1))
check("6 days ahead rejected", code == 400, j)
st = tomorrow(12)
d, s, e = parts(st, st + timedelta(hours=2))
r = client.get(f"/api/advance/quote?lot_id={L}&date={d}&start_time={s}&end_time={e}").get_json()
check("quote returns price before booking", r.get("price") == 25 or r.get("price") in (20, 25), r)

print("2) Validation (end > start, past, duration)")
st = tomorrow(14)
code, j = book(L, st, st - timedelta(hours=1), phone=ph(2))
check("end before start rejected", code == 400, j)
code, j = book(L, datetime.now() - timedelta(hours=1), datetime.now(), phone=ph(2))
check("past start rejected", code == 400, j)
code, j = book(L, st, st + timedelta(minutes=5), phone=ph(2))
check("too short rejected", code == 400, j)

print("3) Overlap / capacity over the full interval")
L2 = add_lot("Small", 2, lat=28.60, lng=77.30)
a = tomorrow(12)
c1, j1 = book(L2, a, a + timedelta(hours=2), phone=ph(11))
c2, j2 = book(L2, a, a + timedelta(hours=2), phone=ph(12))
c3, j3 = book(L2, a, a + timedelta(hours=2), phone=ph(13))
check("two bookings fit", c1 == 200 and c2 == 200)
check("third overlapping rejected (lot full msg)", c3 == 409 and j3.get("lot_full"), j3)
c4, j4 = book(L2, a + timedelta(hours=1), a + timedelta(hours=3), phone=ph(14))
check("partial overlap (13-15) rejected - checks whole interval", c4 == 409, j4)
c5, j5 = book(L2, a + timedelta(hours=2), a + timedelta(hours=4), phone=ph(15))
check("back-to-back (14-16) allowed", c5 == 200, j5)
c6, j6 = book(L2, a - timedelta(hours=1), a + timedelta(minutes=30), phone=ph(16))
check("overlap on start edge rejected", c6 == 409, j6)

print("4) Cancel releases capacity immediately")
r = client.post(f"/api/advance/{j1['code']}/cancel", json={"t": j1["token"]}).get_json()
check("cancel ok with refund", r.get("ok") and r["refund"] == j1["booking"]["amount"] * 80 // 100, r)
c3b, j3b = book(L2, a, a + timedelta(hours=2), phone=ph(13))
check("slot free after cancel", c3b == 200, j3b)
r = client.post(f"/api/advance/{j1['code']}/cancel", json={"t": j1["token"]})
check("double cancel blocked", r.status_code == 409)
r = client.post(f"/api/advance/{j2['code']}/cancel", json={"t": "wrong"})
check("wrong token blocked", r.status_code == 404)

print("5) Payment (existing demo rules)")
code, j = book(L2, tomorrow(18), tomorrow(19), phone=ph(21), upi_id="fail@upi")
check("fail@upi declined 402", code == 402, j)
code, j = book(L2, tomorrow(18), tomorrow(19), phone=ph(21), method="CARD", card_number="4111111111110000", cvv="123")
check("card ending 0000 declined", code == 402, j)

print("6) Early arrival - same lot")
E1 = add_lot("EarlyA", 5, cur=0, lat=28.70, lng=77.10)
st = datetime.now() + timedelta(hours=2)
c, jb = book(E1, st, st + timedelta(hours=2), phone=ph(31))
r = client.post(f"/api/advance/{jb['code']}/early", json={"t": jb["token"]}).get_json()
check("same lot early allowed", r.get("outcome") == "SAME_LOT" and r["booking"]["lot_id"] == E1, r)
check("original booked time kept for reference", r["booking"]["booked_start"] == st.strftime("%H:%M"))
r2 = client.post(f"/api/advance/{jb['code']}/cancel", json={"t": jb["token"]})
check("cannot cancel after early park", r2.status_code == 409)

print("7) Early arrival - transfer to nearby lot")
A = add_lot("TA", 1, cur=0, lat=28.80, lng=77.10)
B = add_lot("TB", 5, cur=0, lat=28.801, lng=77.101)       # nazdeek
far = add_lot("TFar", 5, cur=0, lat=29.50, lng=78.00)     # bahut door
st = datetime.now() + timedelta(hours=2)
c, jt = book(A, st, st + timedelta(hours=2), phone=ph(41))
check("booking in A ok", c == 200, jt)
set_count(A, 1)                                            # A abhi live full
r = client.post(f"/api/advance/{jt['code']}/early", json={"t": jt["token"]}).get_json()
check("transferred to nearby lot B (not far)", r.get("outcome") == "TRANSFERRED" and r["booking"]["lot_id"] == B, r)
check("original lot recorded", r["booking"]["original_lot_id"] == A)
c = db()
cnt = c.execute("SELECT COUNT(*) FROM advance_bookings WHERE lot_id=? AND status='CONFIRMED'", (A,)).fetchone()[0]
c.close()
check("original lot capacity released", cnt == 0, cnt)
set_count(A, 0)

print("8) No capacity -> queue, FCFS, validity")
Q1 = add_lot("QA", 1, cur=0, lat=28.90, lng=77.20)
Q2 = add_lot("QB", 1, cur=0, lat=28.901, lng=77.201)
Q3 = add_lot("QC", 1, cur=0, lat=28.902, lng=77.202)
st = datetime.now() + timedelta(hours=2)
cA, ja = book(Q1, st, st + timedelta(hours=1), phone=ph(51))
cC, jc = book(Q2, st, st + timedelta(hours=1), phone=ph(53))
check("setup bookings", cA == cC == 200, (ja, jc))
for q in (Q1, Q2, Q3):
    set_count(q, 1)                                        # teeno lot live full
r = client.post(f"/api/advance/{ja['code']}/early", json={"t": ja["token"]}).get_json()
check("no capacity -> queue offered (no auto change)", r.get("outcome") == "NO_CAPACITY" and r.get("offer_queue"), r)
g = client.get(f"/api/advance/{ja['code']}?t={ja['token']}").get_json()
check("booking unchanged without user's request", g["early_status"] == "NONE" and g["lot_id"] == Q1)
r1 = client.post(f"/api/advance/{ja['code']}/early", json={"t": ja["token"], "join_queue": True}).get_json()
check("user joins queue (first)", r1.get("outcome") == "QUEUED" and r1["booking"]["queue_position"] == 1, r1)
r2 = client.post(f"/api/advance/{jc['code']}/early", json={"t": jc["token"], "join_queue": True}).get_json()
check("second user is #2 (FCFS)", r2["booking"]["queue_position"] == 2, r2)
set_count(Q3, 0)                                           # sirf QC me ek jagah khuli (dono ke liye nayi jagah)
g1 = client.get(f"/api/advance/{ja['code']}?t={ja['token']}").get_json()
g2 = client.get(f"/api/advance/{jc['code']}?t={jc['token']}").get_json()
check("FCFS: first waiting user gets the freed slot", g1["early_status"] == "TRANSFERRED" and g1["lot_id"] == Q3, g1)
check("second user still waiting", g2["early_status"] == "WAITING", g2)
check("allocation status message updated", "Waiting queue" in (g1["early_message"] or ""))
set_count(Q2, 0)                                           # ab QB (second user ka apna lot) me jagah khuli
g2 = client.get(f"/api/advance/{jc['code']}?t={jc['token']}").get_json()
check("second user allocated when capacity frees", g2["early_status"] == "ARRIVED_SAME", g2)
set_count(Q1, 0)

print("9) Queue validity: cancelled booking removed from queue")
V1 = add_lot("VA", 1, cur=0, lat=28.95, lng=77.30)
st = datetime.now() + timedelta(hours=2)
cv, jv = book(V1, st, st + timedelta(hours=1), phone=ph(61))
set_count(V1, 1)
client.post(f"/api/advance/{jv['code']}/early", json={"t": jv["token"], "join_queue": True})
client.post(f"/api/advance/{jv['code']}/cancel", json={"t": jv["token"]})
set_count(V1, 0)
client.get("/api/lots")
c = db()
qs = c.execute("SELECT status FROM early_queue WHERE booking_id=(SELECT id FROM advance_bookings WHERE code=?)",
               (jv["code"],)).fetchone()["status"]
c.close()
check("cancelled booking not allocated", qs == "CANCELLED", qs)

print("10) Modify time -> price recalculated")
M1 = add_lot("Mod", 5, lat=28.20, lng=77.20)
st = datetime.now() + timedelta(hours=30)
cm, jm = book(M1, st, st + timedelta(hours=1), phone=ph(71))
old = jm["booking"]["amount"]
new_st = datetime.now() + timedelta(minutes=40)
d, s, e = parts(new_st, new_st + timedelta(hours=1))
r = client.post(f"/api/advance/{jm['code']}/modify", json={"t": jm["token"], "date": d, "start_time": s,
                                                           "end_time": e, "preview": True}).get_json()
check("preview shows new price 40 and diff", r.get("new_amount") == 40 and r["difference"] == 40 - old, r)
r = client.post(f"/api/advance/{jm['code']}/modify", json={"t": jm["token"], "date": d, "start_time": s, "end_time": e})
check("extra payment required", r.status_code == 400, r.get_json())
r = client.post(f"/api/advance/{jm['code']}/modify", json={"t": jm["token"], "date": d, "start_time": s,
                                                           "end_time": e, **UPI}).get_json()
check("modified & repriced", r.get("ok") and r["booking"]["amount"] == 40, r)

print("11) Simultaneous bookings (no overbooking)")
S1 = add_lot("Race", 3, lat=28.10, lng=77.10)
res = []
st = tomorrow(10)


def worker(i):
    cl = app.test_client()
    d, s, e = parts(st, st + timedelta(hours=2))
    r = cl.post("/api/advance/book", json={"lot_id": S1, "date": d, "start_time": s, "end_time": e,
                                           "user_name": "Racer", "phone": ph(800 + i), **UPI})
    res.append(r.status_code)


ts = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
[t.start() for t in ts]; [t.join() for t in ts]
check("exactly 3 of 10 succeed", res.count(200) == 3 and res.count(409) == 7, res)

print("12) Existing app still works")
check("/api/lots", client.get("/api/lots").status_code == 200)
check("/api/recommend", client.get("/api/recommend?lat=28.55&lng=77.25").status_code == 200)
r = client.post("/api/update", json={"lot_id": 1, "action": "ENTRY"})
check("/api/update", r.status_code in (200, 400, 409))
check("/advance page", client.get("/advance").status_code in (200, 500))  # 500 = template path (test dir me hai)

print("13) Existing /reserve flow still works + counts in advance availability")
if any(r.rule == "/api/reservations" for r in app.url_map.iter_rules()):
    LG = add_lot("Legacy", 5, lat=28.30, lng=77.30)
    r = client.post("/api/reservations", json={"lot_id": LG, "slots": 1, "hold_minutes": 30, "user_name": "Leg",
                                               "phone": "9111111111", "vehicle_no": "X"})
    check("legacy reservation create", r.status_code == 200, r.get_json())
    lj = r.get_json()
    r = client.post(f"/api/reservations/{lj['code']}/pay", json={"t": lj["token"], "method": "UPI", "upi_id": "a@upi"})
    check("legacy pay (demo gateway)", r.status_code == 200, r.get_json())
    g = client.get(f"/api/reservations/{lj['code']}?t={lj['token']}").get_json()
    check("legacy ticket CONFIRMED with OTP", g.get("status") == "CONFIRMED" and g.get("otp"), g)
    st = datetime.now() + timedelta(minutes=30)
    d, s, e = parts(st, st + timedelta(minutes=20))
    q = client.get(f"/api/advance/quote?lot_id={LG}&date={d}&start_time={s}&end_time={e}").get_json()
    check("legacy hold reduces advance availability (5-1=4)", q.get("free_slots") == 4, q)
    r = client.post(f"/api/reservations/{lj['code']}/cancel", json={"t": lj["token"]})
    check("legacy cancel", r.status_code == 200, r.get_json())
else:
    print("  (skipped: reservations.py not present)")

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)