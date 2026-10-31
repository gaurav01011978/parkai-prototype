"""ParkAI - AI Parking Assistant (chat).

Design:
* Sirf eligible active subscribers (backend check, subscriptions.get_access) is endpoint ko use kar sakte hain.
* Assistant ka har jawab real backend se aata hai: lots/availability/price/booking sab existing APIs
  (/api/lots, /api/recommend, /api/advance/*) ko internally call karke. Kuch bhi invent nahi hota -
  booking confirmation sirf tab dikhta hai jab /api/advance/book success return kare.
* Language understanding rule-based hai (intent + slot filling + follow-up questions), koi external LLM nahi.
* Book/cancel/early/leave-queue se pehle hamesha explicit "yes" confirmation.
* Booking tokens server par store nahi hote: browser har request me apni parkai_adv list ('items') bhejta hai.
"""
import json
import re
import secrets
from datetime import datetime, timedelta, date

from flask import Blueprint, jsonify, request, render_template, current_app

from database import get_conn, FMT
import advance
from subscriptions import get_access

bp = Blueprint("assistant", __name__)

MAX_MESSAGE_LEN = 500
SESSION_TTL_DAYS = 2
FEATURE_LABEL = {"booking": "creating, viewing and cancelling reservations",
                 "early": "early-arrival and waiting-queue help"}


# ============================== DB (safe migration) ==============================
def init_assistant_db():
    conn = get_conn()
    conn.execute("""CREATE TABLE IF NOT EXISTS assistant_sessions (
        id TEXT PRIMARY KEY, sub_code TEXT NOT NULL, state TEXT NOT NULL, updated_at TEXT NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS assistant_usage (
        sub_code TEXT NOT NULL, day TEXT NOT NULL, count INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (sub_code, day))""")
    conn.commit()
    conn.close()


def load_session(sid, sub_code):
    conn = get_conn()
    conn.execute("DELETE FROM assistant_sessions WHERE updated_at < ?",
                 ((datetime.now() - timedelta(days=SESSION_TTL_DAYS)).strftime(FMT),))
    row = conn.execute("SELECT * FROM assistant_sessions WHERE id=? AND sub_code=?", (sid or "", sub_code)).fetchone()
    conn.commit()
    conn.close()
    if row:
        try:
            return row["id"], json.loads(row["state"])
        except ValueError:
            pass
    return secrets.token_urlsafe(12), {}


def save_session(sid, sub_code, state):
    conn = get_conn()
    conn.execute("INSERT INTO assistant_sessions (id, sub_code, state, updated_at) VALUES (?,?,?,?) "
                 "ON CONFLICT(id) DO UPDATE SET state=excluded.state, updated_at=excluded.updated_at",
                 (sid, sub_code, json.dumps(state), datetime.now().strftime(FMT)))
    conn.commit()
    conn.close()


def bump_usage(sub_code, limit):
    """(allowed, used). Limit par pahunchne ke baad count nahi badhta."""
    day = datetime.now().strftime("%Y-%m-%d")
    conn = get_conn()
    row = conn.execute("SELECT count FROM assistant_usage WHERE sub_code=? AND day=?", (sub_code, day)).fetchone()
    used = row["count"] if row else 0
    if used >= limit:
        conn.close()
        return False, used
    conn.execute("INSERT INTO assistant_usage (sub_code, day, count) VALUES (?,?,1) "
                 "ON CONFLICT(sub_code, day) DO UPDATE SET count = count + 1", (sub_code, day))
    conn.commit()
    conn.close()
    return True, used + 1


# ============================== internal API calls (existing endpoints reuse) ==============================
def api(method, path, body=None, params=None):
    c = current_app.test_client()
    r = c.open(path, method=method, json=body, query_string=params)
    return r.status_code, (r.get_json(silent=True) or {})


def get_lots():
    st, data = api("GET", "/api/lots")
    return data if st == 200 and isinstance(data, list) else []


# ============================== text understanding ==============================
MONTHS = {m: i + 1 for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct",
                                          "nov", "dec"])}
WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
MER = r"(a\.?m\.?|p\.?m\.?)"
YES = {"yes", "y", "yeah", "yep", "yup", "confirm", "confirmed", "ok", "okay", "sure", "go ahead", "do it",
       "proceed", "haan", "ha", "han", "haa", "kar do", "yes please", "please do", "book it", "yes confirm"}
NO = {"no", "n", "nope", "nah", "nahi", "nahin", "dont", "don't", "stop", "abort", "never mind", "nevermind",
      "cancel", "not now", "no thanks", "leave it", "mat karo", "rehne do", "forget it"}


def norm(text):
    return re.sub(r"\s+", " ", text.strip().lower())


def is_yes(t):
    return t.strip(" .!") in YES


def is_no(t):
    return t.strip(" .!") in NO


def mer_of(s):
    return None if not s else ("am" if s.lower().startswith("a") else "pm")


def to24(h, m, mer):
    if mer:
        return (h % 12) + (12 if mer == "pm" else 0), m
    return h, m


def strip_noise(t):
    t = re.sub(r"\b\d{4}-\d{2}-\d{2}\b", " ", t)
    t = re.sub(r"\b\d{10}\b", " ", t)
    t = re.sub(r"\bad-[a-z0-9]{6}\b", " ", t)
    return t


def parse_date(t, now):
    t = strip_noise(t) if not re.search(r"\b\d{4}-\d{2}-\d{2}\b", t) else t
    m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", t)
    if m:
        try:
            return date(int(m[1]), int(m[2]), int(m[3]))
        except ValueError:
            return None
    today = now.date()
    if "day after tomorrow" in t or re.search(r"\bparso\b", t):
        return today + timedelta(days=2)
    if re.search(r"\b(tomorrow|tmrw|tmr|tomorow)\b", t):
        return today + timedelta(days=1)
    if re.search(r"\b(today|tonight|aaj|this evening|this morning|this afternoon)\b", t):
        return today
    mm = "|".join(MONTHS)
    m = re.search(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s*(?:of\s+)?({mm})[a-z]*\b", t) or None
    d = mo = None
    if m:
        d, mo = int(m[1]), MONTHS[m[2]]
    else:
        m = re.search(rf"\b({mm})[a-z]*\s+(\d{{1,2}})(?:st|nd|rd|th)?\b", t)
        if m:
            d, mo = int(m[2]), MONTHS[m[1]]
    if d:
        try:
            cand = date(today.year, mo, d)
            if cand < today:
                cand = date(today.year + 1, mo, d)
            return cand
        except ValueError:
            return None
    m = re.search(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b", t)
    if m:
        try:
            y = int(m[3]) + (2000 if m[3] and int(m[3]) < 100 else 0) if m[3] else today.year
            cand = date(y, int(m[2]), int(m[1]))
            if not m[3] and cand < today:
                cand = date(today.year + 1, int(m[2]), int(m[1]))
            return cand
        except ValueError:
            return None
    m = re.search(r"\b(monday|mon|tuesday|tues?|wednesday|wed|thursday|thu|thur|thurs|friday|fri|saturday|sat|"
                  r"sunday|sun)\b", t)
    if m:
        wd = WEEKDAYS[m[1][:3]]
        delta = (wd - today.weekday()) % 7
        if delta == 0 and "next" in t:
            delta = 7
        return today + timedelta(days=delta)
    return None


def parse_times(t, expecting=None):
    """-> dict(start=(h,m,mer)|None, end=(h,m,mer)|None, duration=min|None, rel_start=min|None)."""
    t = strip_noise(t)
    out = {"start": None, "end": None, "duration": None, "rel_start": None}
    m = re.search(r"\bin\s+(\d+)\s*(hours?|hrs?|hr|h|minutes?|mins?|min)\b", t)
    if m:
        out["rel_start"] = int(m[1]) * (60 if m[2].startswith("h") else 1)
        t = t.replace(m[0], " ")
    m = re.search(r"\b(?:for|of)\s+(?:(an?|\d+(?:\.\d+)?)\s*)?(hours?|hrs?|hr|h|minutes?|mins?|min)\b", t)
    if m:
        n = 1.0 if (m[1] in (None, "a", "an")) else float(m[1])
        out["duration"] = int(n * (60 if m[2].startswith("h") else 1))
        t = t.replace(m[0], " ")
    elif re.search(r"\bhalf an hour\b", t):
        out["duration"] = 30
    def ok(h, mi):
        return 0 <= h <= 24 and 0 <= mi < 60
    rng = re.search(rf"(?:from\s+|between\s+)?\b(\d{{1,2}})(?::(\d{{2}}))?\s*{MER}?\s*(?:to|till|until|and|-|–|—)\s*"
                    rf"(\d{{1,2}})(?::(\d{{2}}))?\s*{MER}?(?![a-z0-9])", t)
    if rng and ok(int(rng[1]), int(rng[2] or 0)) and ok(int(rng[4]), int(rng[5] or 0)):
        out["start"] = (int(rng[1]), int(rng[2] or 0), mer_of(rng[3]))
        out["end"] = (int(rng[4]), int(rng[5] or 0), mer_of(rng[6]))
        return out
    toks = []
    for m in re.finditer(rf"\b(\d{{1,2}})(?::(\d{{2}}))?\s*{MER}(?![a-z])", t):
        toks.append((m.start(), (int(m[1]), int(m[2] or 0), mer_of(m[3]))))
    for m in re.finditer(r"\b([01]?\d|2[0-3]):([0-5]\d)\b(?!\s*(?:am|pm|a\.m|p\.m))", t):
        toks.append((m.start(), (int(m[1]), int(m[2]), None)))
    for m in re.finditer(r"\b(?:at|from|around|starting|start|till|until|by)\s+(\d{1,2})\b(?!\s*(?::|am|pm|a\.m|p\.m))",
                         t):
        toks.append((m.start(), (int(m[1]), 0, None)))
    if re.fullmatch(r"\s*(\d{1,2})\s*", t) and expecting in ("time", "end"):
        toks.append((0, (int(t), 0, None)))
    if re.search(r"\bnoon\b", t):
        toks.append((0, (12, 0, "pm")))
    toks = [x[1] for x in sorted(toks) if ok(x[1][0], x[1][1])]
    if toks:
        if expecting == "end" or re.search(r"\b(till|until)\b", t) and len(toks) == 1 and not re.search(
                r"\b(from|at|starting)\b", t):
            out["end"] = toks[0]
        else:
            out["start"] = toks[0]
            if len(toks) > 1:
                out["end"] = toks[1]
    return out


def resolve_range(start, end):
    """raw tuples -> ('HH:MM' ya None, 'HH:MM' ya None, ambiguous_bool)."""
    def has_mer(x):
        return x is None or x[2] is not None or x[0] == 0 or x[0] > 12
    if start and end:
        s, e = list(start), list(end)
        if s[2] is None and e[2] is not None and 1 <= s[0] <= 12:
            s[2] = e[2]
            if to24(*s)[0] * 60 + s[1] >= to24(*e)[0] * 60 + e[1] and e[2] == "pm":
                s[2] = "am"
        elif e[2] is None and s[2] is not None and 1 <= e[0] <= 12:
            e[2] = s[2]
            if to24(*e)[0] * 60 + e[1] <= to24(*s)[0] * 60 + s[1]:
                e[2] = "pm" if s[2] == "am" else "am"
        start, end = tuple(s), tuple(e)
    if not (has_mer(start) and has_mer(end)):
        return None, None, True
    f = lambda x: None if x is None else "%02d:%02d" % to24(*x)[:2]
    return f(start), f(end), False


def fmt12(hhmm):
    h, m = map(int, hhmm.split(":"))
    return "%d:%02d %s" % ((h % 12) or 12, m, "PM" if h >= 12 else "AM")


def fmt_day(d):
    return d.strftime("%a %d %b")


def parse_lot(t, lots, last_ids, expecting=None):
    """Text se lot id nikalo (naam ya 'first/second/option 2' jaisa reference)."""
    for l in lots:
        if re.search(rf"(?<![a-z0-9]){re.escape(l['name'].lower())}(?![a-z0-9])", t):
            return l["id"]
    m = re.search(r"\blot\s*([a-z0-9]+)\b", t)
    if m:
        for l in lots:
            if l["name"].lower().replace("lot", "").strip() == m[1]:
                return l["id"]
    idx = None
    if re.search(r"\b(first|1st|top|best|recommended|that one|this one|the one)\b", t):
        idx = 0
    elif re.search(r"\b(second|2nd)\b", t):
        idx = 1
    elif re.search(r"\b(third|3rd)\b", t):
        idx = 2
    else:
        m = re.search(r"\b(?:option|number|no\.?|#)\s*(\d)\b", t) or (
            re.fullmatch(r"\s*(\d)\s*", t) if expecting == "lot" else None)
        if m:
            idx = int(m[1]) - 1
    if idx is not None and last_ids and 0 <= idx < len(last_ids):
        return last_ids[idx]
    return None


# ============================== replies ==============================
def R(reply, suggestions=None, **extra):
    return {"reply": reply, "suggestions": suggestions or [], **extra}


def money(n):
    return "₹%d" % n


def tiers_text():
    out, prev = [], 0
    for mins, price, label in advance.PRICING_TIERS:
        out.append(f"  • {label}: {money(price)}")
        prev = mins
    return "\n".join(out)


def plan_for_feature(feature):
    conn = get_conn()
    rows = conn.execute("SELECT name, assistant_features FROM subscription_plans WHERE active=1 "
                        "ORDER BY sort_order").fetchall()
    conn.close()
    for r in rows:
        if feature in json.loads(r["assistant_features"] or "[]"):
            return r["name"]
    return None


def locked(ctx, feature):
    if feature in ctx["sub"]["assistant_features"]:
        return None
    need = plan_for_feature(feature)
    have = ctx["sub"]["plan"]["name"] if ctx["sub"]["plan"] else "your"
    msg = f"{FEATURE_LABEL.get(feature, feature).capitalize()} isn't included in the {have} plan."
    if need:
        msg += f" It's available from the {need} plan — you can upgrade on the Plans page (/plans)."
    return R(msg, ["Find parking", "Pricing", "My plan"])


# ============================== FAQ ==============================
def faq_answers():
    cfg = advance
    return [
        (r"what is parkai|about parkai|what does parkai|what can you do|who are you|\bhelp\b|capabilit",
         "I'm the ParkAI assistant. I can:\n"
         "  • find parking using live availability, predicted occupancy and distance\n"
         "  • quote prices and check a time window\n"
         "  • book, show and cancel advance reservations (with your confirmation)\n"
         "  • help with early arrival and the waiting queue\n"
         "Try: “Find parking near me” or “Book Lot A tomorrow 5 to 7 pm”.",
         ["Find parking", "Book a spot", "My bookings", "Pricing"]),
        (r"cancel|refund",
         f"Cancelling a confirmed advance reservation refunds {100 - cfg.CANCEL_FEE_PCT}% of the amount paid "
         f"({cfg.CANCEL_FEE_PCT}% cancellation fee). Once you've parked early (same lot or transferred) it can no "
         "longer be cancelled. To cancel, just say “cancel my booking”.", ["Cancel a booking", "My bookings"]),
        (r"how (do|can|to) .*(book|reserve)|how does (booking|reservation)|how .*advance",
         f"Tell me the lot, date and start/end time (up to {cfg.MAX_ADVANCE_MIN // 1440} days ahead, "
         f"{cfg.MIN_DURATION_MIN} min to {cfg.MAX_DURATION_MIN // 60} h). I'll check real availability and the price, "
         "ask for your name, phone and UPI ID, then show a summary and book only after you say yes.",
         ["Book a spot", "Pricing"]),
        (r"early|arrive.*before|before.*time|too early",
         f"If you arrive early (up to {cfg.EARLY_WINDOW_MIN} min before your start time), ParkAI first checks your "
         f"lot for the time remaining; if it's full it looks for a free spot in lots within "
         f"{cfg.NEARBY_RADIUS_M // 1000} km and moves your booking there. If nothing is free you can join a "
         "first-come-first-served waiting queue and your original booking stays safe.",
         ["I'm here early", "Queue status"]),
        (r"queue|waiting list|fcfs|first come",
         "The early-arrival waiting queue is first-come-first-served. When capacity frees up (e.g. someone cancels) "
         "the person waiting longest is allocated first. Your original booking is never lost while you wait, and "
         "you can leave the queue anytime.", ["Queue status", "Leave queue"]),
        (r"recommend|how .*(rank|choose|pick|suggest)|why .*lot|best lot",
         "I rank lots by three things from live data: free spaces (after reservations), predicted occupancy for the "
         "hour, and distance from you. Free space counts most. Share your location and I'll include distance.",
         ["Find parking"]),
        (r"availab|how many (spots|slots|spaces)|free (spots|slots|space)",
         "Availability = capacity − vehicles inside − active reservations. For a future time I check every "
         "overlapping booking in that window. Ask me “find parking tomorrow 5 to 7 pm”.", ["Find parking"]),
        (r"modify|change.*(time|booking)|reschedul|extend",
         "Changing a booking's time isn't something I can do in chat yet — open the Advance Reservation page "
         "(/advance), where you can modify it (the price is recalculated for the new time).", ["My bookings"]),
        (r"payment|pay\b|upi|card|gateway",
         "For advance reservations I use the same payment step as the Advance Reservation page (a UPI ID such as "
         "name@upi). I never ask for card details in chat.", ["Book a spot"]),
        (r"otp|check.?in|gate",
         "Instant “Reserve” bookings (/reserve) are checked in at the gate with the booking code + OTP shown on "
         "your ticket. Advance reservations just use your booking code.", []),
        (r"modify|support|contact|human",
         "I can't connect you to a human from here, but I can help with parking, bookings and the queue.", []),
    ]


def faq_answer(t):
    for pat, ans, sug in faq_answers():
        if re.search(pat, t):
            return R(ans, sug)
    return None


# ============================== intents ==============================
CODE_RE = re.compile(r"\bad-[a-z0-9]{6}\b", re.I)
QUESTION_START = re.compile(r"^(how|what|why|when|where|which|can|could|does|do|is|are|tell me|explain|will)\b")


def detect_intent(t):
    has_code = bool(CODE_RE.search(t))
    if re.search(r"\b(leave|quit|exit|remove me from|get out of)\b.*\b(queue|waiting)", t):
        return "leave_queue"
    if re.search(r"\b(subscription|my plan|plans?|upgrade|renew|expir|premium|membership)\b", t) and not re.search(
            r"parking plan|park", t):
        return "plan"
    if re.search(r"\b(queue|waiting)\b", t) and re.search(r"\b(status|position|where|my|am i|check|still)\b", t):
        return "queue_status"
    if re.search(r"(i'?m|i am|i have|i've|ive|just)\s+(here|arrived|reached|at the)|(arrived|reached|came|here|am|reach"
                 r"ing) early|i'?m early|early arrival please|reached the (lot|gate)", t):
        return "early"
    if re.search(r"\b(cancel|delete)\b", t) and re.search(r"(book|reserv|slot|spot|parking)", t + " x") and not \
            re.search(r"policy|fee|charge|refund|how|can i|what|will", t):
        return "cancel"
    if re.search(r"\bcancel\b", t) and has_code:
        return "cancel"
    if re.search(r"\b(show|view|list|see|check|display|get)\b.*\b(my )?(booking|reservation|ticket)s?\b|my (booking|"
                 r"reservation)s?|\bstatus\b", t) or (has_code and len(t.split()) <= 3):
        return "view"
    asks_how = bool(QUESTION_START.match(t))
    has_slot_info = bool(re.search(r"\b(lot\s*\w|today|tomorrow|\d\s*(am|pm)|\d:\d\d)", t))
    if re.search(r"\b(book|reserve|reservation|booking)\b", t) and not (asks_how and not has_slot_info):
        return "book"
    if re.search(r"(i (need|want|would like) (a |to )?(park|parking|spot|slot|space))|park my (car|bike|vehicle)", t):
        return "book" if has_slot_info and not re.search(r"\b(find|where|nearest|best)\b", t) else "find"
    if re.search(r"\b(prices?|pricing|cost|costs|rate|rates|how much|charges?|fees?)\b", t) and not re.search(
            r"cancel", t):
        return "price"
    if re.search(r"(find|suggest|recommend|nearest|nearby|best|where|which|available|availability|free|vacant|any|"
                 r"show|search|look).*(park|lot|spot|slot|space)|parking (near|nearby|available)|is there (any )?"
                 r"parking|\bnear me\b|kahan", t):
        return "find"
    if re.fullmatch(r"(hi|hello|hey|hii+|namaste|good (morning|evening|afternoon))\W*", t):
        return "greet"
    if re.fullmatch(r"(thanks|thank you|thx|ty|great|cool|nice|perfect|awesome)\W*", t):
        return "thanks"
    return None


# ============================== flows ==============================
def day_range_text(slots):
    d = date.fromisoformat(slots["date"])
    return f"{fmt_day(d)}, {fmt12(slots['start'])} – {fmt12(slots['end'])}"


def fill_slots_from_text(ctx, t, expecting):
    """Booking slots (lot/date/time) text se bharo. Returns list of notes jab kuch samajh nahi aaya/ambiguous."""
    s = ctx["state"]["slots"]
    now = datetime.now()
    if "lot_id" not in s:
        lid = parse_lot(t, ctx["lots"](), ctx["state"].get("last_lots", []), expecting)
        if lid:
            s["lot_id"] = lid
    d = parse_date(t, now)
    tm = parse_times(t, expecting)
    if tm["rel_start"] is not None:
        st = (now + timedelta(minutes=tm["rel_start"])).replace(second=0, microsecond=0)
        s["date"] = st.strftime("%Y-%m-%d")
        s["start"] = st.strftime("%H:%M")
    elif d:
        s["date"] = d.isoformat()
    # time ranges
    raw_s = tm["start"] if tm["start"] else None
    raw_e = tm["end"] if tm["end"] else None
    pend = ctx["state"].pop("ampm_raw", None) if expecting == "ampm" else None
    if expecting == "ampm" and pend:
        want = "am" if re.search(r"\b(am|morning|a\.m)\b", t) else "pm" if re.search(
            r"\b(pm|evening|afternoon|night|p\.m)\b", t) else None
        if want:
            raw_s = tuple(pend["s"][:2]) + (pend["s"][2] or want,) if pend["s"] else None
            raw_e = tuple(pend["e"][:2]) + (pend["e"][2] or want,) if pend["e"] else None
        else:
            ctx["state"]["ampm_raw"] = pend
            return "ampm"
    if raw_s or raw_e:
        a, b, amb = resolve_range(raw_s, raw_e)
        if amb:
            ctx["state"]["ampm_raw"] = {"s": raw_s, "e": raw_e}
            return "ampm"
        if a and (raw_s is not None):
            s["start"] = a
        if b:
            s["end"] = b
    if tm["duration"] and "start" in s and "end" not in s:
        sd = datetime.strptime(f"{s.get('date', now.strftime('%Y-%m-%d'))} {s['start']}", "%Y-%m-%d %H:%M")
        e = sd + timedelta(minutes=tm["duration"])
        if e.date() == sd.date():
            s["end"] = e.strftime("%H:%M")
        else:
            return "overnight"
    return None


def book_next(ctx):
    """Agla sawal ya confirmation summary. Existing quote API se availability + price."""
    st = ctx["state"]
    s = st["slots"]
    lots = ctx["lots"]()
    if "lot_id" not in s:
        st["expecting"] = "lot"
        names = ", ".join(l["name"] for l in lots)
        return R(f"Which parking lot would you like? Available lots: {names}. "
                 "(Say “find parking” and I'll suggest the best one.)",
                 [l["name"] for l in lots][:4] + ["Find parking"])
    lot = next((l for l in lots if l["id"] == s["lot_id"]), None)
    if lot is None:
        s.pop("lot_id")
        return book_next(ctx)
    if "date" not in s:
        st["expecting"] = "date"
        return R(f"Got it — {lot['name']}. Which day? (today, tomorrow, or a date — up to "
                 f"{advance.MAX_ADVANCE_MIN // 1440} days ahead)", ["Today", "Tomorrow"])
    if "start" not in s:
        st["expecting"] = "time"
        return R(f"What time do you want to park at {lot['name']} on {fmt_day(date.fromisoformat(s['date']))}? "
                 "Give a start and end, e.g. “5 to 7 pm”.")
    if "end" not in s:
        st["expecting"] = "end"
        return R(f"Starting at {fmt12(s['start'])}. When do you leave — or for how long? "
                 "(e.g. “7 pm” or “2 hours”)", ["1 hour", "2 hours", "3 hours"])
    if "quote" not in s:
        code, q = api("GET", "/api/advance/quote", params={"lot_id": s["lot_id"], "date": s["date"],
                                                           "start_time": s["start"], "end_time": s["end"]})
        if code != 200:
            for k in ("date", "start", "end"):
                s.pop(k, None)
            st["expecting"] = "date"
            return R(f"I can't use that time: {q.get('error', 'invalid time')}. Let's pick the day and time again — "
                     "which day?", ["Today", "Tomorrow"])
        if not q.get("available"):
            st["_try"] = {"date": s["date"], "start": s["start"], "end": s["end"]}
            alt = alternatives(ctx, s)
            s.pop("lot_id", None)
            msg = f"{lot['name']} is full for that time."
            if alt:
                st["expecting"] = "lot"   # date/start/end rehte hain; user naya lot chunega
                st["last_lots"] = [a["id"] for a in alt]
                return R(msg + " These lots have space then: " +
                         ", ".join(f"{a['name']} ({a['free']} free, {money(a['price'])})" for a in alt) +
                         ". Which one would you like?", [a["name"] for a in alt])
            for k in ("start", "end"):
                s.pop(k, None)
            st["expecting"] = "date"
            return R(msg + " No other lot has space in that window either. Want to try another day or time?",
                     ["Today", "Tomorrow"])
        s["quote"] = {"price": q["price"], "tier": q["tier"], "free": q["free_slots"]}
    prof = st.setdefault("profile", {})
    if "name" not in prof:
        st["expecting"] = "name"
        q = s["quote"]
        return R(f"Good news — {lot['name']} has space on {day_range_text(s)}. Price: {money(q['price'])} "
                 f"({q['tier']}). What name should the booking be under?")
    if "phone" not in prof:
        st["expecting"] = "phone"
        return R("Your 10-digit phone number?")
    if "vehicle" not in prof:
        st["expecting"] = "vehicle"
        return R("Vehicle number? (optional — say “skip”)", ["Skip"])
    if "upi" not in s:
        st["expecting"] = "upi"
        return R(f"To pay {money(s['quote']['price'])} I'll use the same payment step as the Advance Reservation "
                 "page. Please enter your UPI ID (like name@upi).")
    st["expecting"] = "confirm"
    st["flow"] = "book"
    q = s["quote"]
    veh = prof.get("vehicle") or "not given"
    return R(f"Please confirm this booking:\n"
             f"  • Lot: {lot['name']}\n  • When: {day_range_text(s)}\n  • Price: {money(q['price'])} ({q['tier']})\n"
             f"  • Name: {prof['name']}  • Phone: {prof['phone']}  • Vehicle: {veh}\n"
             f"  • Pay with UPI: {s['upi']}\n"
             "The final price is calculated by ParkAI when the booking is made. Shall I book it? (yes / no)",
             ["Yes, book it", "No"])


def alternatives(ctx, s):
    """Doosre lots jahan [start,end] free ho (quote API se). s['start']/['end'] hata diye gaye hote hain, isliye
    original time ctx me rakha hai."""
    out = []
    t0 = ctx["state"].get("_try")
    if not t0:
        return out
    for l in ctx["lots"]():
        if l["id"] == s.get("lot_id"):
            continue
        c, q = api("GET", "/api/advance/quote", params={"lot_id": l["id"], "date": t0["date"],
                                                         "start_time": t0["start"], "end_time": t0["end"]})
        if c == 200 and q.get("available"):
            out.append({"id": l["id"], "name": l["name"], "start": t0["start"], "end": t0["end"],
                        "free": q["free_slots"], "price": q["price"]})
    out.sort(key=lambda a: -a["free"])
    return out


# ============================== booking flow (input handling) ==============================
def _snapshot(s):
    return {k: s.get(k) for k in ("lot_id", "date", "start", "end")}


def ask_ampm(ctx):
    ctx["state"]["expecting"] = "ampm"
    return R("Just to be sure — is that AM or PM?", ["AM", "PM"])


def after_fill(ctx, res, before, expecting):
    """fill_slots ke baad common handling. Returns reply ya None (None => book_next chalao)."""
    st, s = ctx["state"], ctx["state"]["slots"]
    if res == "ampm":
        return ask_ampm(ctx)
    if res == "overnight":
        st["expecting"] = "end"
        return R("A booking has to start and end on the same day. Please give an end time before midnight.")
    if _snapshot(s) != before:
        s.pop("quote", None)
        return None
    return "nochange"


def flow_book(ctx, t, raw):
    st = ctx["state"]
    s = st["slots"]
    prof = st.setdefault("profile", {})
    exp = st.get("expecting")
    if exp in ("lot", "date", "time", "end", "ampm"):
        before = _snapshot(s)
        res = fill_slots_from_text(ctx, t, exp)
        out = after_fill(ctx, res, before, exp)
        if out == "nochange":
            hint = {"lot": "Sorry, I didn't recognise that lot. Please pick one of the lots listed.",
                    "date": "Sorry, I didn't catch the date. Try “today”, “tomorrow” or a date like 2 Nov.",
                    "time": "Sorry, I didn't catch the time. Try something like “5 to 7 pm”.",
                    "end": "Sorry, I didn't catch that. Give an end time like “7 pm” or a duration like “2 hours”.",
                    "ampm": "Please reply AM or PM."}[exp]
            return R(hint)
        if out:
            return out
        return book_next(ctx)
    if exp == "name":
        n = re.sub(r"^(my name is|i am|i'm|this is|name is|it'?s)\s+", "", raw.strip(), flags=re.I).strip(" .")
        if not re.fullmatch(r"[A-Za-z][A-Za-z .'-]{1,39}", n):
            return R("Please give a name using letters only (2–40 characters).")
        prof["name"] = n.title() if n.islower() else n
        return book_next(ctx)
    if exp == "phone":
        digits = re.sub(r"\D", "", raw)
        if len(digits) == 12 and digits.startswith("91"):
            digits = digits[2:]
        if len(digits) != 10:
            return R("I need a 10-digit phone number (without spaces or country code).")
        prof["phone"] = digits
        return book_next(ctx)
    if exp == "vehicle":
        if t in ("skip", "no", "none", "na", "n/a", "-", "dont have", "don't have"):
            prof["vehicle"] = ""
        else:
            v = re.sub(r"[\s-]", "", raw).upper()
            if not re.fullmatch(r"[A-Z0-9]{4,12}", v):
                return R("That doesn't look like a vehicle number. Try something like DL1AB1234, or say “skip”.",
                         ["Skip"])
            prof["vehicle"] = v
        return book_next(ctx)
    if exp == "upi":
        m = re.search(r"[^\s@]+@[^\s@]+", raw)
        if not m or len(m[0]) < 5:
            return R("That doesn't look like a UPI ID. It should look like name@upi.")
        s["upi"] = m[0].lower()
        return book_next(ctx)
    if exp == "confirm":
        if is_yes(t):
            return do_book(ctx)
        if is_no(t):
            reset_flow(ctx)
            return R("Okay — I haven't booked anything. Tell me if you'd like to try a different lot or time.",
                     ["Find parking", "Book a spot"])
        return R("Please reply “yes” to book or “no” to cancel this booking request.", ["Yes, book it", "No"])
    reset_flow(ctx)
    return R("Let's start again. What would you like to do?", ["Find parking", "Book a spot", "My bookings"])


def reset_flow(ctx):
    st = ctx["state"]
    for k in ("flow", "expecting", "slots", "ampm_raw", "pick", "_try"):
        st.pop(k, None)
    st["slots"] = {}


def do_book(ctx):
    st = ctx["state"]
    s, prof = st["slots"], st["profile"]
    body = {"lot_id": s["lot_id"], "date": s["date"], "start_time": s["start"], "end_time": s["end"],
            "user_name": prof["name"], "phone": prof["phone"], "vehicle_no": prof.get("vehicle", ""),
            "method": "UPI", "upi_id": s["upi"]}
    code, d = api("POST", "/api/advance/book", body)
    if code == 200 and d.get("ok"):
        b = d["booking"]
        reset_flow(ctx)
        st["window"] = None
        return R(f"Booked ✅  Your code is {d['code']}.\n"
                 f"  • Lot: {b['lot_name']}\n  • When: {fmt_day(date.fromisoformat(b['date']))}, "
                 f"{fmt12(b['start'])} – {fmt12(b['end'])}\n  • Paid: {money(b['amount'])} ({b['price_tier']})\n"
                 f"  • Payment ref: {b['payment_ref']}\n"
                 "It's saved in this browser, so I can show or cancel it for you later.",
                 ["My bookings", "Pricing"], store_booking={"code": d["code"], "token": d["token"]})
    msg = d.get("error", "The booking could not be completed.")
    if d.get("lot_full"):
        for k in ("lot_id", "quote"):
            s.pop(k, None)
        st["expecting"] = "lot"
        return R(f"I couldn't book it — {msg} Nothing was charged. Pick another lot?",
                 [l["name"] for l in ctx["lots"]()][:4])
    if code == 402 or "UPI" in msg:
        s.pop("upi", None)
        st["expecting"] = "upi"
        return R(f"The payment didn't go through: {msg} Nothing was booked. Please enter a different UPI ID.")
    if "phone" in msg.lower() and code == 400:
        prof.pop("phone", None)
        st["expecting"] = "phone"
        return R(f"{msg} What's your phone number?")
    if code == 409:
        reset_flow(ctx)
        return R(f"I couldn't book it: {msg} Nothing was booked.", ["My bookings"])
    for k in ("date", "start", "end", "quote"):
        s.pop(k, None)
    st["expecting"] = "date"
    return R(f"I couldn't book it: {msg} Nothing was booked. Which day would you like instead?",
             ["Today", "Tomorrow"])


def start_book(ctx, t):
    lk = locked(ctx, "booking")
    if lk:
        return lk
    st = ctx["state"]
    reset_flow(ctx)
    st["flow"] = "book"
    s = st["slots"]
    has_time = bool(parse_date(t, datetime.now()) or any(v for k, v in parse_times(t).items()))
    w = st.get("window")
    if w and not has_time:     # "find parking tomorrow 5-7" ke baad "book the first one"
        s.update({"date": w["date"], "start": w["start"], "end": w["end"]})
    res = fill_slots_from_text(ctx, t, None)
    if res == "ampm":
        return ask_ampm(ctx)
    if res == "overnight":
        st["expecting"] = "end"
        return R("A booking has to start and end on the same day. Please give an end time before midnight.")
    return book_next(ctx)


# ============================== find / pricing ==============================
def find_next(ctx):
    """Find flow: window poora ho to availability search, warna follow-up."""
    st = ctx["state"]
    s = st["slots"]
    if "start" in s and "date" not in s:
        s["date"] = datetime.now().strftime("%Y-%m-%d")
    if "date" in s and "start" not in s:
        st["expecting"] = "time"
        return R(f"What time on {fmt_day(date.fromisoformat(s['date']))}? e.g. “5 to 7 pm”.")
    if "start" in s and "end" not in s:
        st["expecting"] = "end"
        return R(f"Starting at {fmt12(s['start'])} — until when, or for how long?", ["1 hour", "2 hours"])
    if "start" in s:
        res = find_window(ctx, s["date"], s["start"], s["end"])
    else:
        res = find_now(ctx)
    reset_flow(ctx)
    return res


def flow_find(ctx, t, raw):
    st = ctx["state"]
    exp = st.get("expecting")
    before = _snapshot(st["slots"])
    res = fill_slots_from_text(ctx, t, exp)
    out = after_fill(ctx, res, before, exp)
    if out == "nochange":
        return R("Sorry, I didn't catch that. Give a time like “5 to 7 pm”, or say “now” to see live availability.",
                 ["Now"]) if not re.search(r"\bnow\b", t) else _done_now(ctx)
    return out or find_next(ctx)


def _done_now(ctx):
    r = find_now(ctx)
    reset_flow(ctx)
    return r


def start_find(ctx, t):
    st = ctx["state"]
    reset_flow(ctx)
    st["flow"] = "find"
    res = fill_slots_from_text(ctx, t, None)
    if res == "ampm":
        return ask_ampm(ctx)
    if res == "overnight":
        st["expecting"] = "end"
        return R("Please give an end time before midnight.")
    s = st["slots"]
    s.pop("lot_id", None)
    return find_next(ctx)


def find_now(ctx):
    st = ctx["state"]
    loc = ctx.get("loc")
    if loc:
        code, rows = api("GET", "/api/recommend", params={"lat": loc[0], "lng": loc[1]})
        if code == 200 and isinstance(rows, list) and rows:
            st["last_lots"] = [r["id"] for r in rows]
            st["window"] = None
            lines = [f"  {i + 1}. {r['name']} — {r['free']} free of {r['capacity']}, ~{r['predicted_occupancy']}% "
                     f"predicted occupancy, {r['distance_m']} m away" for i, r in enumerate(rows[:3])]
            best = rows[0]
            why = recommend_reason(best, rows[1] if len(rows) > 1 else None)
            if best["free"] == 0:
                return R("Right now every lot is full:\n" + "\n".join(lines) +
                         "\nI can check a later time window for you — when do you plan to park?",
                         ["Tomorrow 5 to 7 pm"])
            return R("Here's what I found right now, ranked for your location:\n" + "\n".join(lines) +
                     f"\n\nMy pick: {best['name']}. {why}\nWant me to book it, or check a specific time?",
                     ["Book the first one", "Check tomorrow 5 to 7 pm"])
    lots = sorted(ctx["lots"](), key=lambda l: -l["free"])
    if not lots:
        return R("I couldn't load the parking lots right now. Please try again in a moment.")
    st["last_lots"] = [l["id"] for l in lots]
    st["window"] = None
    lines = [f"  {i + 1}. {l['name']} — {l['free']} free of {l['capacity']}" for i, l in enumerate(lots[:3])]
    if lots[0]["free"] == 0:
        return R("Right now every lot is full:\n" + "\n".join(lines) + "\nWant me to check a later time?",
                 ["Tomorrow 5 to 7 pm"])
    return R("Live availability (most free space first):\n" + "\n".join(lines) +
             f"\n\n{lots[0]['name']} has the most free space right now. I don't have your location, so distance "
             "isn't included — use “Use my location” and ask again for a distance-aware pick, or tell me a time "
             "window to check.", ["Book the first one", "Check tomorrow 5 to 7 pm"])


def recommend_reason(best, nxt):
    bits = [f"it has {best['free']} free spaces", f"~{best['predicted_occupancy']}% predicted occupancy",
            f"it's {best['distance_m']} m away"]
    why = "Reason: " + ", ".join(bits) + "."
    if nxt:
        diffs = []
        if best["free"] > nxt["free"]:
            diffs.append(f"{best['free'] - nxt['free']} more free spaces than {nxt['name']}")
        if best["distance_m"] < nxt["distance_m"]:
            diffs.append(f"{nxt['distance_m'] - best['distance_m']} m closer")
        if best["predicted_occupancy"] < nxt["predicted_occupancy"]:
            diffs.append("lower predicted occupancy")
        if diffs:
            why += f" Compared with {nxt['name']}: " + ", ".join(diffs) + "."
    return why


def find_window(ctx, d, a, b):
    st = ctx["state"]
    lots = ctx["lots"]()
    avail, full, err_msg = [], [], None
    loc = ctx.get("loc")
    for l in lots:
        code, q = api("GET", "/api/advance/quote", params={"lot_id": l["id"], "date": d, "start_time": a,
                                                           "end_time": b})
        if code != 200:
            err_msg = q.get("error")
            break
        dist = None
        if loc and l.get("lat") is not None:
            dist = round(advance.distance_m(loc[0], loc[1], l["lat"], l["lng"]))
        (avail if q.get("available") else full).append({"l": l, "free": q["free_slots"], "price": q["price"],
                                                        "tier": q["tier"], "dist": dist})
    if err_msg:
        return R(f"I can't check that time: {err_msg} Try another day or time.", ["Today", "Tomorrow"])
    when = f"{fmt_day(date.fromisoformat(d))}, {fmt12(a)} – {fmt12(b)}"
    if not avail:
        return R(f"All lots are full for {when}. Want me to check a different time or day?", ["Tomorrow", "Today"])
    avail.sort(key=lambda x: (-x["free"], x["dist"] if x["dist"] is not None else 0))
    st["last_lots"] = [x["l"]["id"] for x in avail]
    st["window"] = {"date": d, "start": a, "end": b}
    lines = [f"  {i + 1}. {x['l']['name']} — {x['free']} free for that window, {money(x['price'])}"
             + (f", {x['dist']} m away" if x["dist"] is not None else "") for i, x in enumerate(avail[:3])]
    top = avail[0]
    why = f"{top['l']['name']} has the most free space in that window"
    if len(avail) > 1:
        why += f" ({top['free']} vs {avail[1]['free']} at {avail[1]['l']['name']})"
    if full:
        why += "; " + ", ".join(x["l"]["name"] for x in full) + (" is" if len(full) == 1 else " are") + " full then"
    return R(f"Availability for {when}:\n" + "\n".join(lines) + f"\n\n{why}. Price: {money(top['price'])} "
             f"({top['tier']}).\nShall I book {top['l']['name']}?", ["Book the first one"])


def answer_price(ctx, t):
    now = datetime.now()
    d = parse_date(t, now)
    tm = parse_times(t)
    if tm["start"] or d or tm["rel_start"]:
        a, _, amb = resolve_range(tm["start"], None) if tm["start"] else (None, None, False)
        if tm["rel_start"] is not None:
            st = now + timedelta(minutes=tm["rel_start"])
        elif a:
            st = datetime.strptime(f"{(d or now.date()).isoformat()} {a}", "%Y-%m-%d %H:%M")
        elif d and not amb:
            st = datetime.combine(d, now.time())
        else:
            st = None
        if st:
            price, tier = advance.price_for(st, now)
            if price is None or st < now:
                return R("I can only price bookings that start within the next "
                         f"{advance.MAX_ADVANCE_MIN // 1440} days.")
            return R(f"A booking starting {fmt_day(st.date())} at {fmt12(st.strftime('%H:%M'))} costs "
                     f"{money(price)} ({tier}). It's a flat charge per booking, not per hour.", ["Book a spot"])
    return R("Advance reservation pricing depends on how soon the booking starts (flat per booking, not per "
             "hour):\n" + tiers_text() + f"\nBook earlier and it's cheaper. Cancelling refunds "
             f"{100 - advance.CANCEL_FEE_PCT}%. Tell me a time and I'll quote it exactly.",
             ["Book a spot", "Find parking"])


# ============================== my bookings / cancel / early / queue ==============================
def fetch_mine(ctx):
    items = ctx["items"]
    if not items:
        return []
    code, data = api("POST", "/api/advance/mine", {"items": items})
    return data if code == 200 and isinstance(data, list) else []


def token_for(ctx, code):
    return next((i["token"] for i in ctx["items"] if i["code"] == code), "")


def b_line(b):
    s = f"{b['code']} — {b['lot_name']}, {fmt_day(date.fromisoformat(b['date']))} {fmt12(b['start'])}–{fmt12(b['end'])}, " \
        f"{money(b['amount'])}, {b['status'].replace('_', ' ').lower()}"
    es = b["early_status"]
    if es == "WAITING":
        s += f" (early-arrival queue position {b['queue_position']})" if b.get("queue_position") else " (in queue)"
    elif es == "ARRIVED_SAME":
        s += " (parked early)"
    elif es == "TRANSFERRED":
        s += f" (moved from {b['original_lot_name']})"
    return s


def show_bookings(ctx):
    lk = locked(ctx, "booking")
    if lk:
        return lk
    mine = fetch_mine(ctx)
    if not mine:
        return R("I don't see any advance reservations saved in this browser yet. Want to make one?",
                 ["Book a spot", "Find parking"])
    live = [b for b in mine if b["status"] == "CONFIRMED"]
    other = [b for b in mine if b["status"] != "CONFIRMED"]
    lines = ["Active reservations:"] + [f"  • {b_line(b)}" for b in live] if live else ["No active reservations."]
    if other:
        lines += ["Past / cancelled:"] + [f"  • {b_line(b)}" for b in other[:3]]
    return R("\n".join(lines), ["Cancel a booking", "I'm here early"] if live else ["Book a spot"])


def pick_booking(ctx, kind, t):
    """cancel / early / leave: candidates dhoondo; ek chuna ho to confirmation maango, kai ho to pooch lo."""
    st = ctx["state"]
    need = "early" if kind in ("early", "leave") else "booking"
    lk = locked(ctx, need)
    if lk:
        return lk
    mine = fetch_mine(ctx)
    if kind == "cancel":
        cands = [b for b in mine if b["can_cancel"]]
        none_msg = "You have no active reservation that can be cancelled."
    elif kind == "early":
        cands = [b for b in mine if b["can_early"]]
        none_msg = None
    else:
        cands = [b for b in mine if b["early_status"] == "WAITING"]
        none_msg = "You're not in the early-arrival waiting queue."
    m = CODE_RE.search(t)
    if m:
        code = m[0].upper()
        hit = next((b for b in mine if b["code"] == code), None)
        if hit is None:
            return R(f"I can't find {code} among the reservations saved in this browser.")
        if hit not in cands:
            return R(f"{code} can't be used for that right now ({hit['status'].replace('_', ' ').lower()}"
                     f"{', already parked early' if hit['early_status'] in ('ARRIVED_SAME', 'TRANSFERRED') else ''}).")
        cands = [hit]
    if not cands:
        if kind == "early":
            ups = [b for b in mine if b["status"] == "CONFIRMED" and b["early_status"] == "NONE"]
            parked = [b for b in mine if b["early_status"] in ("ARRIVED_SAME", "TRANSFERRED")]
            if parked:
                return R(f"You've already been allowed in early: {b_line(parked[0])}.")
            if ups:
                nxt = sorted(ups, key=lambda b: (b["date"], b["start"]))[0]
                return R(f"Early arrival can only be requested within {advance.EARLY_WINDOW_MIN} minutes before your "
                         f"start time (and before it begins). Your next booking is {b_line(nxt)}.",
                         ["My bookings"])
            return R("I don't see an upcoming reservation to use for early arrival. Want to make one?",
                     ["Book a spot"])
        return R(none_msg, ["Book a spot", "My bookings"])
    st["flow"] = kind
    st["pick"] = {"codes": [b["code"] for b in cands]}
    if len(cands) > 1:
        st["expecting"] = "pick"
        lines = [f"  {i + 1}. {b_line(b)}" for i, b in enumerate(cands)]
        verb = {"cancel": "cancel", "early": "check in early", "leave": "remove from the queue"}[kind]
        return R(f"Which one do you want to {verb}? Reply with the number or the code.\n" + "\n".join(lines))
    return ask_confirm_action(ctx, kind, cands[0])


def ask_confirm_action(ctx, kind, b):
    st = ctx["state"]
    st["expecting"] = "confirm"
    st["pick"] = {"codes": [b["code"]], "chosen": b["code"]}
    if kind == "cancel":
        refund = b["amount"] * (100 - advance.CANCEL_FEE_PCT) // 100
        return R(f"Cancel this reservation?\n  • {b_line(b)}\nYou paid {money(b['amount'])}; the expected refund is "
                 f"{money(refund)} ({advance.CANCEL_FEE_PCT}% fee). This can't be undone. (yes / no)",
                 ["Yes, cancel it", "No, keep it"])
    if kind == "early":
        return R(f"I'll check for space now for {b_line(b)}. If {b['lot_name']} is full I'll look for a free lot "
                 f"nearby and, if you agree, move your booking there. Check now? (yes / no)", ["Yes", "No"])
    return R(f"Leave the early-arrival queue? Your original booking ({b_line(b)}) stays valid. (yes / no)",
             ["Yes", "No"])


def flow_pick(ctx, t, raw):
    st = ctx["state"]
    kind, exp = st["flow"], st.get("expecting")
    pk = st.get("pick", {})
    if exp == "pick":
        m = CODE_RE.search(t)
        code = m[0].upper() if m else None
        if not code:
            n = re.fullmatch(r"\D*(\d)\D*", t)
            if n and 1 <= int(n[1]) <= len(pk["codes"]):
                code = pk["codes"][int(n[1]) - 1]
        if code not in pk.get("codes", []):
            return R("Please reply with the number or the booking code from the list above.")
        mine = {b["code"]: b for b in fetch_mine(ctx)}
        if code not in mine:
            reset_flow(ctx)
            return R("I couldn't load that booking any more.")
        return ask_confirm_action(ctx, kind, mine[code])
    code = pk.get("chosen")
    if exp == "queue":     # early: koi jagah nahi, queue join karna hai?
        if is_yes(t):
            return do_early(ctx, code, join_queue=True)
        reset_flow(ctx)
        return R("Okay — I haven't added you to the queue. Your original booking is unchanged.", ["My bookings"])
    if is_yes(t):
        if kind == "cancel":
            return do_cancel(ctx, code)
        if kind == "early":
            return do_early(ctx, code, join_queue=False)
        return do_leave(ctx, code)
    if is_no(t):
        reset_flow(ctx)
        return R("Okay — nothing has been changed.", ["My bookings"])
    return R("Please reply “yes” or “no”.", ["Yes", "No"])


def do_cancel(ctx, code):
    c, d = api("POST", f"/api/advance/{code}/cancel", {"t": token_for(ctx, code)})
    reset_flow(ctx)
    if c == 200 and d.get("ok"):
        return R(f"Cancelled ✅  {code} is cancelled. Refund: {money(d['refund'])}.", ["My bookings", "Book a spot"])
    return R(f"I couldn't cancel {code}: {d.get('error', 'unknown error')}. Nothing was changed.", ["My bookings"])


def do_early(ctx, code, join_queue):
    c, d = api("POST", f"/api/advance/{code}/early", {"t": token_for(ctx, code), "join_queue": join_queue})
    st = ctx["state"]
    out = d.get("outcome")
    b = d.get("booking")
    if c == 200 and out == "SAME_LOT":
        reset_flow(ctx)
        return R(f"You can park now at {b['lot_name']} ✅  Your booking {code} now runs "
                 f"{fmt12(b['start'])} – {fmt12(b['end'])}.", ["My bookings"])
    if c == 200 and out == "TRANSFERRED":
        reset_flow(ctx)
        return R(f"{b['original_lot_name']} is full, so your booking {code} has been moved to {b['lot_name']} ✅  "
                 f"It now runs {fmt12(b['start'])} – {fmt12(b['end'])} there.", ["My bookings"])
    if c == 200 and out == "QUEUED":
        reset_flow(ctx)
        pos = b.get("queue_position")
        return R(f"You're in the early-arrival queue" + (f" at position {pos}" if pos else "") +
                 f". As soon as space frees up you'll be allocated (first come, first served). Your original booking "
                 "is safe; ask me “queue status” anytime.", ["Queue status", "Leave queue"])
    if out == "NO_CAPACITY":
        st["expecting"] = "queue"
        st["pick"] = {"codes": [code], "chosen": code}
        return R("There's no free space right now in your lot or any lot nearby. Want me to put you in the "
                 "first-come-first-served early-arrival waiting queue? Your original booking stays safe. (yes / no)",
                 ["Yes, join queue", "No"])
    reset_flow(ctx)
    return R(f"Early arrival isn't possible for {code}: {d.get('error', 'unknown error')}", ["My bookings"])


def do_leave(ctx, code):
    c, d = api("POST", f"/api/advance/{code}/early/leave", {"t": token_for(ctx, code)})
    reset_flow(ctx)
    if c == 200 and d.get("ok"):
        return R("You've left the waiting queue. Your original booking is still valid.", ["My bookings"])
    return R(f"I couldn't update the queue: {d.get('error', 'unknown error')}.")


def queue_status(ctx):
    lk = locked(ctx, "early")
    if lk:
        return lk
    mine = fetch_mine(ctx)
    waiting = [b for b in mine if b["early_status"] == "WAITING"]
    if waiting:
        return R("\n".join(f"{b['code']} ({b['lot_name']}): queue position {b['queue_position']}" for b in waiting) +
                 "\nYou'll be allocated automatically when space frees up.", ["Leave queue", "My bookings"])
    done = [b for b in mine if b["early_status"] in ("ARRIVED_SAME", "TRANSFERRED")]
    if done:
        return R(f"You're not waiting. {b_line(done[0])}. You can park now.", ["My bookings"])
    return R("You're not in the early-arrival queue right now.", ["I'm here early", "My bookings"])


def plan_info(ctx, used):
    sub = ctx["sub"]
    p = sub["plan"]
    feats = {"qa": "parking Q&A", "search": "lot search", "booking": "booking & cancelling",
             "early": "early arrival & queue help"}
    msg = (f"You're on the {p['name']} plan ({sub['status'].lower()}), valid until {sub['expires_at']} "
           f"({sub['days_left']} days left). Assistant features: "
           + ", ".join(feats.get(f, f) for f in sub["assistant_features"]) +
           f". Messages today: {used}/{sub['daily_message_limit']}.")
    if sub["status"] == "CANCELLED":
        msg += " Renewal is off — access ends on the date above."
    if sub["expiring_soon"]:
        msg += " It expires soon — renew on the Plans page (/plans)."
    return R(msg + "\nManage, renew or upgrade on the Plans page (/plans).")


# ============================== dispatcher ==============================
FALLBACK = ("I'm not sure I understood. I can find parking, quote prices, and book, show or cancel reservations. "
            "What would you like to do?")
MENU = ["Find parking", "Book a spot", "My bookings", "Pricing"]


def respond(ctx, text, used=0):
    t = norm(text)
    st = ctx["state"]
    st.setdefault("slots", {})
    if re.fullmatch(r"(reset|start over|restart|clear|new chat)\W*", t):
        reset_flow(ctx)
        return R("Okay, starting fresh. What can I do for you?", MENU)
    flow, exp = st.get("flow"), st.get("expecting")
    free_text = exp in ("name", "phone", "vehicle", "upi")
    if flow:
        abort = is_no(t) and exp not in ("confirm", "queue", "vehicle")
        if abort or re.fullmatch(r"(stop|never mind|nevermind|cancel that|forget it)\W*", t):
            reset_flow(ctx)
            return R("Okay, I've dropped that. Nothing was booked or changed.", MENU)
        intent = None if free_text or exp in ("confirm", "queue", "pick") else detect_intent(t)
        if intent in ("cancel", "view", "early", "leave_queue", "queue_status", "plan", "find", "book") and \
                not (intent == "book" and flow == "book") and not (intent == flow):
            if intent == "find" and flow == "book" and exp == "lot":
                pass
            reset_flow(ctx)
            flow = None
        else:
            if flow == "book":
                return flow_book(ctx, t, text)
            if flow == "find":
                return flow_find(ctx, t, text)
            return flow_pick(ctx, t, text)
    intent = detect_intent(t)
    if intent == "greet":
        return R(f"Hi {ctx['sub']['name'].split()[0]}! I'm your ParkAI assistant. What would you like to do?", MENU)
    if intent == "thanks":
        return R("You're welcome! Anything else?", MENU)
    if intent == "find":
        return start_find(ctx, t)
    if intent == "book":
        return start_book(ctx, t)
    if intent == "cancel":
        return pick_booking(ctx, "cancel", t)
    if intent == "view":
        m = CODE_RE.search(t)
        if m:
            lk = locked(ctx, "booking")
            if lk:
                return lk
            hit = next((b for b in fetch_mine(ctx) if b["code"] == m[0].upper()), None)
            return R(b_line(hit), ["Cancel a booking"]) if hit else R(
                f"I can't find {m[0].upper()} among the reservations saved in this browser.")
        return show_bookings(ctx)
    if intent == "early":
        return pick_booking(ctx, "early", t)
    if intent == "leave_queue":
        return pick_booking(ctx, "leave", t)
    if intent == "queue_status":
        return queue_status(ctx)
    if intent == "price":
        return answer_price(ctx, t)
    if intent == "plan":
        return plan_info(ctx, used)
    f = faq_answer(t)
    if f:
        return f
    if parse_lot(t, ctx["lots"](), []) and (parse_date(t, datetime.now()) or parse_times(t)["start"]):
        return start_book(ctx, t)
    return R(FALLBACK, MENU)


# ============================== routes ==============================
@bp.route("/assistant")
def assistant_page():
    return render_template("assistant.html")


def _clean_items(raw):
    out = []
    for it in (raw or [])[:50]:
        if isinstance(it, dict) and isinstance(it.get("code"), str) and isinstance(it.get("token"), str):
            out.append({"code": it["code"][:20], "token": it["token"][:64]})
    return out


def _loc(d):
    try:
        lat, lng = float(d.get("lat")), float(d.get("lng"))
    except (TypeError, ValueError):
        return None
    return (lat, lng) if -90 <= lat <= 90 and -180 <= lng <= 180 else None


@bp.route("/api/assistant/chat", methods=["POST"])
def chat():
    d = request.get_json(silent=True) or {}
    # Backend subscription check - har message par
    sub, e = get_access(d.get("sub_code"), d.get("sub_token"))
    if e:
        return jsonify({"error": e["error"], "code": e["code"]}), e["status"]
    text = str(d.get("message") or "").strip()
    if not text:
        return jsonify({"error": "Message khaali hai"}), 400
    if len(text) > MAX_MESSAGE_LEN:
        return jsonify({"error": f"Message {MAX_MESSAGE_LEN} characters se chhota rakho"}), 400
    allowed, used = bump_usage(sub["code"], sub["daily_message_limit"])
    if not allowed:
        return jsonify({"error": f"Aaj ki {sub['daily_message_limit']} messages ki limit poori ho gayi. "
                                 "Kal dobara try karo ya plan upgrade karo.", "code": "DAILY_LIMIT"}), 429
    sid, state = load_session(d.get("session_id"), sub["code"])
    cache = {}

    def lots():
        if "v" not in cache:
            cache["v"] = get_lots()
        return cache["v"]
    ctx = {"sub": sub, "state": state, "items": _clean_items(d.get("items")), "loc": _loc(d), "lots": lots}
    try:
        res = respond(ctx, text, used)
    except Exception:
        current_app.logger.exception("assistant failure")
        reset_flow(ctx)
        res = R("Sorry, something went wrong on my side. Nothing was booked or changed. Please try again.", MENU)
    save_session(sid, sub["code"], state)
    return jsonify({**res, "session_id": sid, "usage": {"used": used, "limit": sub["daily_message_limit"]}})