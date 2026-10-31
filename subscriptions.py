"""ParkAI - Subscription plans (Basic / Pro / Premium).
 
* Plans DB table me rehte hain (configurable): defaults neeche DEFAULT_PLANS me, admin API se badal sakte ho.
* Identity: existing bookings jaisa hi (code + token). Koi login nahi.
* Payment: existing ParkAI demo gateway (advance.check_payment: UPI / CARD rules) - advance booking jaisa hi.
  Asli paise nahi katte. Bad/declined UPI-card par plan activate NAHI hota.
  Optional env var: ADMIN_API_KEY (plans edit karne ke liye)
"""
import hmac
import json
import os
import re
import secrets
from datetime import datetime, timedelta
 
from flask import Blueprint, jsonify, request, render_template
 
import advance
from database import get_conn, get_tx_conn, tx_begin, tx_commit, tx_rollback, FMT
 
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:      # python-dotenv optional
    pass
 
bp = Blueprint("subscriptions", __name__)
 
# ===================== CONFIG =====================
BILLING = {"monthly": {"days": 30, "col": "price_monthly", "label": "month"},
           "yearly": {"days": 365, "col": "price_yearly", "label": "year"}}
EXPIRING_SOON_DAYS = 5
 
# assistant_features: "qa" (sawal-jawab), "search" (lot dhoondhna), "booking" (book/view/cancel),
#                     "early" (early arrival + waiting queue)
DEFAULT_PLANS = [
    dict(id="basic", name="Basic", price_monthly=99, price_yearly=999, sort_order=1, daily_message_limit=30,
         tagline="Ask ParkAI anything",
         features=["AI assistant: parking Q&A", "Smart lot search with live availability",
                   "30 assistant messages per day"],
         assistant_features=["qa", "search"]),
    dict(id="pro", name="Pro", price_monthly=249, price_yearly=2499, sort_order=2, daily_message_limit=150,
         tagline="Book and manage by chat",
         features=["Everything in Basic", "Create, view and cancel reservations in chat",
                   "150 assistant messages per day"],
         assistant_features=["qa", "search", "booking"]),
    dict(id="premium", name="Premium", price_monthly=499, price_yearly=4999, sort_order=3, daily_message_limit=1000,
         tagline="The full ParkAI concierge",
         features=["Everything in Pro", "Early-arrival and waiting-queue guidance",
                   "1000 assistant messages per day"],
         assistant_features=["qa", "search", "booking", "early"]),
]
# ==================================================
 
 
def err(msg, status=400, **extra):
    return jsonify({"error": msg, **extra}), status
 
 
def now_dt():
    return datetime.now().replace(microsecond=0)
 
 
def fmt(dt):
    return dt.strftime(FMT)
 
 
def parse_ts(s):
    return datetime.strptime(s, FMT)
 
 
# ---------------------------- DB setup (safe migration: sirf naye tables) ----------------------------
def init_subscription_db():
    conn = get_conn()
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS subscription_plans (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        tagline TEXT,
        price_monthly INTEGER NOT NULL,
        price_yearly INTEGER,
        daily_message_limit INTEGER NOT NULL DEFAULT 30,
        features TEXT NOT NULL DEFAULT '[]',
        assistant_features TEXT NOT NULL DEFAULT '[]',
        sort_order INTEGER NOT NULL DEFAULT 0,
        active INTEGER NOT NULL DEFAULT 1
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS subscribers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT UNIQUE NOT NULL,
        token TEXT NOT NULL,
        name TEXT NOT NULL,
        phone TEXT NOT NULL,
        email TEXT,
        plan_id TEXT,
        billing TEXT,
        status TEXT NOT NULL DEFAULT 'NONE',
        started_at TEXT,
        expires_at TEXT,
        cancelled_at TEXT,
        created_at TEXT NOT NULL
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS subscription_payments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id TEXT UNIQUE NOT NULL,
        payment_id TEXT UNIQUE,
        sub_code TEXT NOT NULL,
        plan_id TEXT NOT NULL,
        billing TEXT NOT NULL,
        kind TEXT NOT NULL,
        amount_paise INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'CREATED',
        created_at TEXT NOT NULL,
        paid_at TEXT,
        period_start TEXT,
        period_end TEXT
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_sub_phone ON subscribers(phone)")
    if c.execute("SELECT COUNT(*) FROM subscription_plans").fetchone()[0] == 0:
        for p in DEFAULT_PLANS:
            c.execute("""INSERT INTO subscription_plans (id, name, tagline, price_monthly, price_yearly,
                         daily_message_limit, features, assistant_features, sort_order) VALUES (?,?,?,?,?,?,?,?,?)""",
                      (p["id"], p["name"], p["tagline"], p["price_monthly"], p["price_yearly"],
                       p["daily_message_limit"], json.dumps(p["features"]), json.dumps(p["assistant_features"]),
                       p["sort_order"]))
    conn.commit()
    conn.close()
 
 
# ---------------------------- helpers ----------------------------
def plan_view(r):
    return {"id": r["id"], "name": r["name"], "tagline": r["tagline"],
            "price_monthly": r["price_monthly"], "price_yearly": r["price_yearly"],
            "daily_message_limit": r["daily_message_limit"],
            "features": json.loads(r["features"] or "[]"),
            "assistant_features": json.loads(r["assistant_features"] or "[]")}
 
 
def get_plan(conn, plan_id):
    return conn.execute("SELECT * FROM subscription_plans WHERE id=? AND active=1", (plan_id,)).fetchone()
 
 
def token_ok(row, t):
    return row is not None and hmac.compare_digest(row["token"].encode(), (t or "").encode())
 
 
def expire_subscribers(conn):
    """Time khatam hui subscriptions ko EXPIRED mark karo. Write-transaction/connection ke andar chalao."""
    conn.execute("UPDATE subscribers SET status='EXPIRED' "
                 "WHERE status IN ('ACTIVE','CANCELLED') AND expires_at <= ?", (fmt(now_dt()),))
 
 
def is_eligible(row):
    return (row is not None and row["status"] in ("ACTIVE", "CANCELLED")
            and row["expires_at"] is not None and row["expires_at"] > fmt(now_dt()))
 
 
def sub_view(conn, row):
    """Public view (token kabhi nahi)."""
    elig = is_eligible(row)
    plan = conn.execute("SELECT * FROM subscription_plans WHERE id=?", (row["plan_id"],)).fetchone() \
        if row["plan_id"] else None
    pv = plan_view(plan) if plan else None
    days_left = None
    if row["expires_at"]:
        days_left = max(0, (parse_ts(row["expires_at"]) - now_dt()).days) if elig else 0
    status = row["status"]
    if status in ("ACTIVE", "CANCELLED") and not elig:
        status = "EXPIRED"
    return {"code": row["code"], "name": row["name"], "status": status, "eligible": elig,
            "auto_renew": False,
            "plan": pv, "billing": row["billing"], "started_at": row["started_at"],
            "expires_at": row["expires_at"], "days_left": days_left,
            "expiring_soon": bool(elig and days_left is not None and days_left <= EXPIRING_SOON_DAYS),
            "cancelled_at": row["cancelled_at"],
            "assistant_features": pv["assistant_features"] if (pv and elig) else [],
            "daily_message_limit": pv["daily_message_limit"] if (pv and elig) else 0}
 
 
def get_access(code, token):
    """Assistant ke liye: (sub_view, error_dict). Backend validation - UI par bharosa nahi."""
    conn = get_tx_conn()
    try:
        tx_begin(conn)
        expire_subscribers(conn)
        row = conn.execute("SELECT * FROM subscribers WHERE code=?", (str(code or ""),)).fetchone()
        if not token_ok(row, str(token or "")):
            tx_commit(conn)
            return None, {"error": "Subscription nahi mili. Plan lo ya sahi account use karo.",
                          "code": "SUBSCRIPTION_REQUIRED", "status": 401}
        view = sub_view(conn, row)
        tx_commit(conn)
    except Exception:
        tx_rollback(conn)
        raise
    finally:
        conn.close()
    if not view["eligible"]:
        why = "expire ho chuki hai" if view["status"] == "EXPIRED" else "active nahi hai"
        return view, {"error": f"Aapki subscription {why}. AI Assistant ke liye plan renew karo.",
                      "code": "SUBSCRIPTION_REQUIRED", "status": 402}
    return view, None
 
 
# ---------------------------- payment (existing demo gateway) ----------------------------
 
def activate(conn, pay, payment_id):
    """Paid order se subscription activate/renew. Transaction ke andar chalao. Idempotent."""
    if pay["status"] == "PAID":
        return False
    sub = conn.execute("SELECT * FROM subscribers WHERE code=?", (pay["sub_code"],)).fetchone()
    now = now_dt()
    days = BILLING[pay["billing"]]["days"]
    same_plan_active = is_eligible(sub) and sub["plan_id"] == pay["plan_id"]
    start = parse_ts(sub["expires_at"]) if same_plan_active else now      # renewal = time extend, plan change = abhi se
    end = start + timedelta(days=days)
    started_at = sub["started_at"] if same_plan_active and sub["started_at"] else fmt(now)
    conn.execute("UPDATE subscribers SET plan_id=?, billing=?, status='ACTIVE', started_at=?, expires_at=?, "
                 "cancelled_at=NULL WHERE id=?", (pay["plan_id"], pay["billing"], started_at, fmt(end), sub["id"]))
    conn.execute("UPDATE subscription_payments SET status='PAID', payment_id=?, paid_at=?, period_start=?, "
                 "period_end=? WHERE id=?", (payment_id, fmt(now), fmt(start), fmt(end), pay["id"]))
    return True
 
 
# ---------------------------- PAGE ----------------------------
@bp.route("/plans")
def plans_page():
    return render_template("plans.html")
 
 
# ---------------------------- APIs ----------------------------
@bp.route("/api/subscriptions/plans")
def plans():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM subscription_plans WHERE active=1 ORDER BY sort_order").fetchall()
    conn.close()
    return jsonify({"plans": [plan_view(r) for r in rows],
                    "billing": {k: {"days": v["days"], "label": v["label"]} for k, v in BILLING.items()},
                    "gateway": {"provider": "demo", "configured": True}})
 
 
@bp.route("/api/subscriptions/checkout", methods=["POST"])
def checkout():
    """Ek step me: payment check (existing demo gateway rules) -> sirf success par subscription activate."""
    d = request.get_json(silent=True) or {}
    billing = d.get("billing", "monthly")
    if billing not in BILLING:
        return err("billing monthly ya yearly hona chahiye")
    pay_err = advance.check_payment(d)      # wahi UPI/CARD rules jo advance booking me hain
    if pay_err:
        return err(*pay_err)
 
    conn = get_tx_conn()
    try:
        tx_begin(conn)
        expire_subscribers(conn)
        plan = get_plan(conn, str(d.get("plan_id", "")))
        if plan is None:
            tx_rollback(conn)
            return err("Plan nahi mila", 404)
        price = plan[BILLING[billing]["col"]]
        if not price or price <= 0:
            tx_rollback(conn)
            return err("Is plan ka ye billing option available nahi hai")
        token = None
        code = str(d.get("code") or "")
        if code:
            sub = conn.execute("SELECT * FROM subscribers WHERE code=?", (code,)).fetchone()
            if not token_ok(sub, str(d.get("t") or "")):
                tx_rollback(conn)
                return err("Subscription nahi mili", 404)
            if is_eligible(sub):
                kind = "RENEW" if sub["plan_id"] == plan["id"] else "CHANGE"
            else:
                kind = "NEW"
        else:
            name = (d.get("name") or "").strip()
            phone = (d.get("phone") or "").strip()
            email = (d.get("email") or "").strip()
            if len(name) < 2:
                tx_rollback(conn)
                return err("Naam likho")
            if not (phone.isdigit() and len(phone) == 10):
                tx_rollback(conn)
                return err("10 digit ka phone number likho")
            if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
                tx_rollback(conn)
                return err("Sahi email likho")
            alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
            code = "SUB-" + "".join(secrets.choice(alphabet) for _ in range(6))
            token = secrets.token_urlsafe(16)
            conn.execute("INSERT INTO subscribers (code, token, name, phone, email, status, created_at) "
                         "VALUES (?,?,?,?,?, 'NONE', ?)", (code, token, name, phone, email or None, fmt(now_dt())))
            kind = "NEW"
        ref = "SUBPAY" + secrets.token_hex(4).upper()
        conn.execute("INSERT INTO subscription_payments (order_id, sub_code, plan_id, billing, kind, amount_paise, "
                     "status, created_at) VALUES (?,?,?,?,?,?, 'CREATED', ?)",
                     (ref, code, plan["id"], billing, kind, int(price) * 100, fmt(now_dt())))
        pay = conn.execute("SELECT * FROM subscription_payments WHERE order_id=?", (ref,)).fetchone()
        activate(conn, pay, ref)
        sub = conn.execute("SELECT * FROM subscribers WHERE code=?", (code,)).fetchone()
        view = sub_view(conn, sub)
        tx_commit(conn)
    except Exception:
        tx_rollback(conn)
        raise
    finally:
        conn.close()
    out = {"ok": True, "kind": kind, "payment_ref": ref, "amount": int(price), "sub_code": code,
           "subscription": view}
    if token:
        out["token"] = token       # sirf naye subscriber ko, ek hi baar
    return jsonify(out)
 
 
@bp.route("/api/subscriptions/<code>")
def status(code):
    conn = get_tx_conn()
    try:
        tx_begin(conn)
        expire_subscribers(conn)
        row = conn.execute("SELECT * FROM subscribers WHERE code=?", (code,)).fetchone()
        if not token_ok(row, request.args.get("t", "")):
            tx_commit(conn)
            return err("Subscription nahi mili", 404)
        view = sub_view(conn, row)
        hist = conn.execute("SELECT plan_id, billing, kind, amount_paise, paid_at, period_end FROM "
                            "subscription_payments WHERE sub_code=? AND status='PAID' ORDER BY id DESC LIMIT 10",
                            (code,)).fetchall()
        tx_commit(conn)
    except Exception:
        tx_rollback(conn)
        raise
    finally:
        conn.close()
    view["payments"] = [dict(h) for h in hist]
    return jsonify(view)
 
 
@bp.route("/api/subscriptions/<code>/cancel", methods=["POST"])
def cancel(code):
    d = request.get_json(silent=True) or {}
    conn = get_tx_conn()
    try:
        tx_begin(conn)
        expire_subscribers(conn)
        row = conn.execute("SELECT * FROM subscribers WHERE code=?", (code,)).fetchone()
        if not token_ok(row, str(d.get("t") or "")):
            tx_rollback(conn)
            return err("Subscription nahi mili", 404)
        if row["status"] != "ACTIVE":
            tx_rollback(conn)
            return err("Sirf active subscription cancel ho sakti hai", 409)
        # Cancel = renew band. Paid period (expires_at) tak access rehta hai, refund nahi.
        conn.execute("UPDATE subscribers SET status='CANCELLED', cancelled_at=? WHERE id=?",
                     (fmt(now_dt()), row["id"]))
        row = conn.execute("SELECT * FROM subscribers WHERE id=?", (row["id"],)).fetchone()
        view = sub_view(conn, row)
        tx_commit(conn)
    except Exception:
        tx_rollback(conn)
        raise
    finally:
        conn.close()
    return jsonify({"ok": True, "subscription": view,
                    "message": f"Subscription cancel ho gayi. Access {view['expires_at']} tak rahega, refund nahi milta."})
 
 
# ---------------------------- Admin: plans configure karo ----------------------------
def admin_ok():
    key = os.environ.get("ADMIN_API_KEY", "").strip()
    return bool(key) and hmac.compare_digest(key.encode(), request.headers.get("X-Admin-Key", "").encode())
 
 
@bp.route("/api/admin/subscription-plans/<plan_id>", methods=["PUT"])
def admin_update_plan(plan_id):
    if not admin_ok():
        return err("Admin key chahiye (ADMIN_API_KEY set karo, header X-Admin-Key bhejo)", 403)
    d = request.get_json(silent=True) or {}
    conn = get_conn()
    row = conn.execute("SELECT * FROM subscription_plans WHERE id=?", (plan_id,)).fetchone()
    if row is None:
        conn.close()
        return err("Plan nahi mila", 404)
    sets, args = [], []
    for col in ("name", "tagline"):
        if col in d:
            sets.append(f"{col}=?"); args.append(str(d[col]))
    for col in ("price_monthly", "price_yearly", "daily_message_limit", "sort_order", "active"):
        if col in d:
            try:
                v = int(d[col])
            except (TypeError, ValueError):
                conn.close()
                return err(f"{col} number hona chahiye")
            if v < 0:
                conn.close()
                return err(f"{col} negative nahi ho sakta")
            sets.append(f"{col}=?"); args.append(v)
    for col in ("features", "assistant_features"):
        if col in d:
            if not isinstance(d[col], list):
                conn.close()
                return err(f"{col} list hona chahiye")
            sets.append(f"{col}=?"); args.append(json.dumps([str(x) for x in d[col]]))
    if sets:
        conn.execute(f"UPDATE subscription_plans SET {', '.join(sets)} WHERE id=?", (*args, plan_id))
        conn.commit()
    row = conn.execute("SELECT * FROM subscription_plans WHERE id=?", (plan_id,)).fetchone()
    conn.close()
    return jsonify({"ok": True, "plan": plan_view(row)})