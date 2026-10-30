import json
import threading
import urllib.request
import urllib.error

BASE = "http://127.0.0.1:5000"
LOT_ID = 2   # Lot B


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


lots = call("GET", "/api/lots")[1]
free = [l for l in lots if l["id"] == LOT_ID][0]["free"]
N = free + 6
print(f"Lot {LOT_ID}: {free} slots free. {N} log ek saath 1-1 slot book kar rahe hain...")

barrier = threading.Barrier(N)
results = []


def worker(i):
    barrier.wait()   # sab ek hi pal par shuru
    status, data = call("POST", "/api/reservations", {
        "lot_id": LOT_ID, "slots": 1, "hold_minutes": 30,
        "user_name": f"User{i}", "phone": "9%09d" % i, "vehicle_no": f"TEST{i}"})
    results.append((status, data))


threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
for t in threads:
    t.start()
for t in threads:
    t.join()

ok = [d for s, d in results if s == 200]
bays = [b for d in ok for b in d["bays"]]
print(f"Successful bookings : {len(ok)}   (expected {free})")
print(f"Rejected            : {len(results) - len(ok)}")
print(f"Bays alag-alag hain : {len(bays) == len(set(bays))}   {sorted(bays)}")

# saaf-safai: sab test bookings cancel kar do
for d in ok:
    call("POST", f"/api/reservations/{d['code']}/cancel", {"t": d["token"]})
print("Test bookings cancel kar di gayin.")