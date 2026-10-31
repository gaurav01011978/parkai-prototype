"""Run: python test_assistant_subscriptions.py   (temporary DB; aapka parkai.db touch nahi hota)
Payment existing demo gateway (UPI/CARD rules) se hota hai."""
import os, sys, tempfile, json
from datetime import datetime, timedelta

import database
database.DB_NAME = os.path.join(tempfile.mkdtemp(), "test.db")

from app import app          # noqa: E402

PASS = FAIL = 0
def check(name, cond, extra=""):
    global PASS, FAIL
    if cond: PASS += 1; print("  PASS", name)
    else: FAIL += 1; print("  FAIL", name, extra)

c = app.test_client()
UPI = {"method": "UPI", "upi_id": "test@upi"}
def db(): return database.get_conn()

print("== Subscriptions ==")
r = c.get("/api/subscriptions/plans").get_json()
check("3 plans seeded", [p["id"] for p in r["plans"]] == ["basic", "pro", "premium"])
base = {"plan_id": "pro", "billing": "monthly", "name": "Asha Rao", "phone": "9876543210"}
check("no payment method -> rejected, nothing created", c.post("/api/subscriptions/checkout", json=base).status_code == 400
      and db().execute("select count(*) from subscribers").fetchone()[0] == 0)
r = c.post("/api/subscriptions/checkout", json={**base, "method": "UPI", "upi_id": "fail@upi"})
check("declined UPI (402) does NOT activate", r.status_code == 402 and db().execute("select count(*) from subscribers").fetchone()[0] == 0)
r = c.post("/api/subscriptions/checkout", json={**base, "method": "CARD", "card_number": "4111111111110000", "cvv": "123"})
check("declined card (402) does NOT activate", r.status_code == 402)
check("bad phone rejected", c.post("/api/subscriptions/checkout", json={**base, "phone": "12", **UPI}).status_code == 400)
check("unknown plan 404", c.post("/api/subscriptions/checkout", json={**base, "plan_id": "gold", **UPI}).status_code == 404)
r = c.post("/api/subscriptions/checkout", json={**base, **UPI}).get_json()
code, tok = r["sub_code"], r["token"]
check("pay -> Pro active, amount from DB (249)", r["ok"] and r["amount"] == 249 and r["subscription"]["eligible"] and r["subscription"]["plan"]["id"] == "pro")
exp1 = r["subscription"]["expires_at"]
check("expiry ~30 days out", 29 <= (datetime.strptime(exp1, "%Y-%m-%d %H:%M:%S") - datetime.now()).days + 1 <= 30)
check("token not exposed in status", tok not in json.dumps(c.get(f"/api/subscriptions/{code}?t={tok}").get_json()))
check("status wrong token 404", c.get(f"/api/subscriptions/{code}?t=bad").status_code == 404)
r = c.post("/api/subscriptions/checkout", json={"plan_id": "pro", "billing": "monthly", "code": code, "t": "bad", **UPI})
check("renew with wrong token rejected", r.status_code == 404)
r = c.post("/api/subscriptions/checkout", json={"plan_id": "pro", "billing": "monthly", "code": code, "t": tok, **UPI}).get_json()
d1, d2 = datetime.strptime(exp1, "%Y-%m-%d %H:%M:%S"), datetime.strptime(r["subscription"]["expires_at"], "%Y-%m-%d %H:%M:%S")
check("renewal stacks 30 more days", r["kind"] == "RENEW" and (d2 - d1).days == 30)
r = c.post("/api/subscriptions/checkout", json={"plan_id": "premium", "billing": "yearly", "code": code, "t": tok, **UPI}).get_json()
check("plan change: premium yearly 4999, starts now ~365d", r["kind"] == "CHANGE" and r["amount"] == 4999 and (datetime.strptime(r["subscription"]["expires_at"], "%Y-%m-%d %H:%M:%S") - datetime.now()).days >= 364)
r = c.post("/api/assistant/chat", json={"message": "hi"})
check("assistant blocked for anonymous (401)", r.status_code == 401)
r = c.post(f"/api/subscriptions/{code}/cancel", json={"t": "wrong"})
check("cancel wrong token 404", r.status_code == 404)
r = c.post(f"/api/subscriptions/{code}/cancel", json={"t": tok}).get_json()
check("cancel keeps access till expiry", r["ok"] and r["subscription"]["status"] == "CANCELLED" and r["subscription"]["eligible"])
conn = db(); conn.execute("UPDATE subscribers SET expires_at=? WHERE code=?", ((datetime.now() - timedelta(minutes=1)).strftime(database.FMT), code)); conn.commit(); conn.close()
st = c.get(f"/api/subscriptions/{code}?t={tok}").get_json()
check("expired after time passes", st["status"] == "EXPIRED" and st["eligible"] is False)
check("assistant blocked after expiry (402)", c.post("/api/assistant/chat", json={"message": "hi", "sub_code": code, "sub_token": tok}).status_code == 402)
r = c.post("/api/subscriptions/checkout", json={"plan_id": "basic", "code": code, "t": tok, **UPI}).get_json()
check("renew after expiry reactivates", r["kind"] == "NEW" and r["subscription"]["eligible"])
check("admin plan edit refused without key", c.put("/api/admin/subscription-plans/basic", json={"price_monthly": 1}).status_code == 403)
os.environ["ADMIN_API_KEY"] = "k"
r = c.put("/api/admin/subscription-plans/basic", json={"price_monthly": 129}, headers={"X-Admin-Key": "k"})
check("admin can configure price", r.status_code == 200 and r.get_json()["plan"]["price_monthly"] == 129)
check("existing manual features untouched (free users)", c.get("/api/lots").status_code == 200 and c.get("/api/recommend?lat=28.55&lng=77.25").status_code == 200)

# ---------------- Assistant ----------------
print("== Assistant ==")
def new_sub(plan, name="Ravi Kumar"):
    r = c.post("/api/subscriptions/checkout", json={"plan_id": plan, "name": name, "phone": "9000000001", **UPI}).get_json()
    assert r["ok"], r
    return r["sub_code"], r["token"]

class Chat:
    def __init__(self, plan, **kw):
        self.code, self.tok = new_sub(plan); self.sid = None; self.items = []; self.kw = kw
    def say(self, msg, **extra):
        r = c.post("/api/assistant/chat", json={"message": msg, "session_id": self.sid, "sub_code": self.code, "sub_token": self.tok, "items": self.items, **self.kw, **extra})
        j = r.get_json(); self.status = r.status_code
        if r.status_code == 200:
            self.sid = j["session_id"]
            if j.get("store_booking"): self.items.append(j["store_booking"])
        return j.get("reply", j.get("error", "")), j

premium = Chat("premium")
basic = Chat("basic")
pro = Chat("pro")

rep, _ = premium.say("hello")
check("greets by name", "Ravi" in rep)
rep, j = premium.say("what is the cancellation policy?")
check("FAQ cancel policy uses real config (20% fee)", "20%" in rep and "80%" in rep)
rep, _ = premium.say("how do I book?")
check("FAQ how to book", "confirm" in rep.lower() or "yes" in rep.lower())
rep, _ = premium.say("how does early arrival work")
check("FAQ early arrival from config (180 min)", "180" in rep)
rep, _ = premium.say("what are your prices")
check("pricing lists real tiers", "₹40" in rep and "₹25" in rep and "₹20" in rep)

lots = {l["name"]: l for l in c.get("/api/lots").get_json()}
rep, _ = premium.say("find parking")
check("find uses live data (Lot A 26 free)", "Lot A" in rep and "26 free" in rep)
check("find without location says so", "location" in rep.lower())
rep, _ = premium.say("find parking near me", lat=28.5514, lng=77.2543)
check("find with location uses /api/recommend + explains", "m away" in rep and "My pick" in rep and "Reason" in rep)

# window search
tm = datetime.now() + timedelta(days=1)
rep, _ = premium.say("find parking tomorrow 5 to 7 pm")
check("window search returns availability + price", "free for that window" in rep and "₹" in rep, rep)
# book via chat
rep, _ = premium.say("book the first one")
check("book follows from search: asks name", "name" in rep.lower(), rep)
rep, _ = premium.say("Ravi Kumar")
rep, _ = premium.say("9876501234")
check("asks vehicle", "vehicle" in rep.lower())
rep, _ = premium.say("dl1ab1234")
check("asks UPI", "UPI" in rep)
before = db().execute("select count(*) from advance_bookings").fetchone()[0]
rep, _ = premium.say("ravi@upi")
check("summary asks confirmation, nothing booked yet", "yes" in rep.lower() and db().execute("select count(*) from advance_bookings").fetchone()[0] == before, rep)
rep, j = premium.say("what?")
check("non yes/no at confirm re-prompts", "yes" in rep.lower() and db().execute("select count(*) from advance_bookings").fetchone()[0] == before)
rep, j = premium.say("yes")
row = db().execute("select * from advance_bookings order by id desc limit 1").fetchone()
check("booking created only after yes, code from backend", row is not None and row["code"] in rep and "Booked" in rep and j.get("store_booking", {}).get("code") == row["code"], rep)
check("booking data correct (17:00-19:00, name, vehicle)", row["start_time"].endswith("17:00:00") and row["end_time"].endswith("19:00:00") and row["user_name"] == "Ravi Kumar" and row["vehicle_no"] == "DL1AB1234")
code1 = row["code"]

rep, _ = premium.say("show my bookings")
check("view lists booking", code1 in rep)

# failed payment is not turned into success
rep, _ = premium.say("book lot b tomorrow 8 to 9 pm")
check("slot-filling: lot+time extracted, profile reused -> asks UPI", "UPI" in rep, rep)
rep, _ = premium.say("fail@upi")
rep, _ = premium.say("yes")
nb = db().execute("select count(*) from advance_bookings").fetchone()[0]
check("declined UPI => no booking, honest message, asks new UPI", "didn't go through" in rep and "Booked" not in rep and nb == before + 1, rep)
rep, _ = premium.say("good@upi"); rep, _ = premium.say("yes")
check("retry with other UPI books", "Booked" in rep)
code2 = db().execute("select code from advance_bookings order by id desc limit 1").fetchone()[0]

# ambiguity follow-ups
rep, _ = premium.say("book lot c tomorrow 5 to 6")
check("asks AM or PM when ambiguous", "AM or PM" in rep, rep)
rep, _ = premium.say("pm")
check("resolves PM and continues", "UPI" in rep or "quote" in rep.lower() or "Price" in rep or "confirm" in rep.lower(), rep)
rep, _ = premium.say("no")
check("abort leaves nothing booked", "haven't booked" in rep or "dropped" in rep)

# past time handled by backend validation
rep, _ = premium.say("book lot a today 1 am to 2 am")
rep, _ = premium.say("yes") if "UPI" in rep else (rep, None)
check("past/invalid time not booked (backend error relayed)", db().execute("select count(*) from advance_bookings").fetchone()[0] == before + 2, rep)
premium.say("reset")

# duration / missing info follow-ups
rep, _ = premium.say("I want to book")
check("asks which lot", "Which parking lot" in rep)
rep, _ = premium.say("Lot A")
check("asks day", "Which day" in rep)
rep, _ = premium.say("tomorrow")
check("asks time", "time" in rep.lower())
rep, _ = premium.say("at 3pm")
check("asks end/duration", "leave" in rep.lower() or "how long" in rep.lower())
rep, _ = premium.say("for 2 hours")
check("duration -> quote -> asks (profile known) UPI", "UPI" in rep, rep)
premium.say("stop")

# cancel flow
rep, _ = premium.say("cancel my booking")
check("cancel with 2 active asks which", "Which one" in rep and code1 in rep and code2 in rep)
rep, _ = premium.say("1")
check("cancel asks confirmation w/ refund estimate", "expected refund" in rep and "yes" in rep.lower())
rep, _ = premium.say("no")
check("cancel declined -> still confirmed", db().execute("select status from advance_bookings where code=?", (code1,)).fetchone()[0] == "CONFIRMED")
rep, _ = premium.say(f"cancel {code2}")
rep, _ = premium.say("yes")
st2 = db().execute("select status, refund_amount, amount from advance_bookings where code=?", (code2,)).fetchone()
check("cancel executed after yes; refund from backend", st2["status"] == "CANCELLED" and f"₹{st2['refund_amount']}" in rep, rep)

# early arrival: lot A currently has space -> make booking start within window
c2 = Chat("premium")
conn = db(); lid = conn.execute("select id from lots where name='Lot A'").fetchone()[0]; conn.close()
s = datetime.now() + timedelta(minutes=90); e = s + timedelta(hours=1)
if e.date() != s.date(): s = datetime.now().replace(hour=22, minute=0) if False else s
r = c.post("/api/advance/book", json={"lot_id": lid, "date": s.strftime("%Y-%m-%d"), "start_time": s.strftime("%H:%M"), "end_time": e.strftime("%H:%M") if e.date()==s.date() else "23:59", "user_name": "Ravi", "phone": "9000000001", "method": "UPI", "upi_id": "a@upi"})
if r.status_code == 200:
    c2.items.append({"code": r.get_json()["code"], "token": r.get_json()["token"]})
    ec = r.get_json()["code"]
    rep, _ = c2.say("I'm here early")
    check("early: asks confirmation first", "yes" in rep.lower() and "Check now" in rep, rep)
    rep, _ = c2.say("yes")
    check("early: same-lot allocation reported from backend", "park now" in rep and db().execute("select early_status from advance_bookings where code=?", (ec,)).fetchone()[0] == "ARRIVED_SAME", rep)
    rep, _ = c2.say("queue status")
    check("queue status when not waiting", "not waiting" in rep or "not in" in rep, rep)
else:
    print("  SKIP early test (booking near midnight)", r.get_json())

# early + full -> queue
c3 = Chat("premium")
conn = db(); conn.execute("UPDATE lots SET current_count=capacity"); conn.commit(); conn.close()
# all lots full live -> need a booking whose start is > horizon (so it could be booked): create when free then fill
conn = db(); conn.execute("UPDATE lots SET current_count=0"); conn.commit(); conn.close()
s = datetime.now() + timedelta(minutes=120); e = s + timedelta(hours=1)
if e.date() != s.date(): e = s.replace(hour=23, minute=59)
r = c.post("/api/advance/book", json={"lot_id": lid, "date": s.strftime("%Y-%m-%d"), "start_time": s.strftime("%H:%M"), "end_time": e.strftime("%H:%M"), "user_name": "Qa Tester", "phone": "9000000002", "method": "UPI", "upi_id": "q@upi"})
if r.status_code == 200 and e > s:
    qc = r.get_json()["code"]; c3.items.append({"code": qc, "token": r.get_json()["token"]})
    conn = db(); conn.execute("UPDATE lots SET current_count=capacity"); conn.commit(); conn.close()
    rep, _ = c3.say("I've arrived early"); rep, _ = c3.say("yes")
    check("early with no capacity offers queue (does not auto-join)", "waiting queue" in rep and db().execute("select early_status from advance_bookings where code=?", (qc,)).fetchone()[0] == "NONE", rep)
    rep, _ = c3.say("yes")
    check("queue joined after yes with real position", "position" in rep and db().execute("select early_status from advance_bookings where code=?", (qc,)).fetchone()[0] == "WAITING", rep)
    rep, _ = c3.say("what's my queue status")
    check("queue status shows position", "queue position" in rep, rep)
    rep, _ = c3.say("leave the queue"); rep, _ = c3.say("yes")
    check("leave queue after confirm", db().execute("select early_status from advance_bookings where code=?", (qc,)).fetchone()[0] == "NONE", rep)
    conn = db(); conn.execute("UPDATE lots SET current_count=0"); conn.commit(); conn.close()
else:
    print("  SKIP queue test", r.status_code, r.get_json())

# plan gating
rep, _ = basic.say("what is parking?")
rep, _ = basic.say("find parking")
check("Basic can search", "Lot" in rep)
rep, _ = basic.say("book lot a tomorrow 5 to 7 pm")
check("Basic blocked from booking via assistant, told which plan", "isn't included" in rep and "Pro" in rep, rep)
rep, _ = basic.say("show my bookings")
check("Basic blocked from viewing", "isn't included" in rep)
rep, _ = pro.say("I'm here early")
check("Pro blocked from early help (Premium)", "Premium" in rep, rep)
rep, _ = pro.say("my plan")
check("plan info", "Pro plan" in rep and "days left" in rep, rep)

# limits & validation
conn = db(); conn.execute("UPDATE subscription_plans SET daily_message_limit=3 WHERE id='basic'"); conn.commit(); conn.close()
lim = Chat("basic")
for _ in range(3): lim.say("hi")
rep, _ = lim.say("hi")
check("daily limit enforced (429)", lim.status == 429)
r = c.post("/api/assistant/chat", json={"message": "x" * 600, "sub_code": premium.code, "sub_token": premium.tok})
check("too long message rejected", r.status_code == 400)
other = Chat("premium")
r = c.post("/api/assistant/chat", json={"message": "show my bookings", "session_id": premium.sid, "sub_code": other.code, "sub_token": other.tok})
check("session of another user not reusable", r.status_code == 200 and r.get_json()["session_id"] != premium.sid)
check("tokens never in assistant replies", premium.tok not in json.dumps(premium.say("my plan")[1]))
rep, _ = premium.say("asdf qwerty")
check("unknown input -> helpful fallback", "not sure" in rep.lower())

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)