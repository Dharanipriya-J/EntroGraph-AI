"""Deterministic synthetic data and labelled evaluation cases (scenario 'default-v1').

Everything is synthetic. A fixed seed and hand-written ring definitions make the graph identical
on every run. NOTE for honesty: the rings, hard negatives and numbers were authored by the
project author, so evaluation results measure the mechanism on THIS data, not real-world accuracy.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .store import GraphStore

SCENARIO_ID = "default-v1"
SOURCE = f"synthetic:{SCENARIO_ID}"
SEED = 20261017
NOW = int(datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc).timestamp())
DAY = 86400

CATEGORIES = ["electronics", "books", "home", "fashion", "grocery", "sports", "beauty", "toys", "gift_cards", "travel"]
PRICE = {"electronics": 240, "books": 18, "home": 60, "fashion": 55, "grocery": 35, "sports": 70,
         "beauty": 28, "toys": 30, "gift_cards": 100, "travel": 320}
HOUR_BUCKET_START = {"night": 1, "morning": 8, "afternoon": 14, "evening": 20}
PRIVATE_KINDS = ("phone", "laptop", "tablet", "smart_tv")


@dataclass
class Case:
    customer_id: str
    label: int  # 1 = planted coordinated-abuse member, 0 = benign
    group: str
    note: str
    expected_counterparts: tuple[str, ...] = ()  # customers a correct traversal should reach
    min_hops_needed: int = 0  # customer-hops needed to reach all expected counterparts


@dataclass
class Scenario:
    customers: list = field(default_factory=list)
    devices: dict = field(default_factory=dict)
    ips: dict = field(default_factory=dict)
    products: dict = field(default_factory=dict)
    sessions: list = field(default_factory=list)
    events: list = field(default_factory=list)
    orders: list = field(default_factory=list)
    cases: list = field(default_factory=list)
    _seq: int = 0
    _ev: int = 0

    def next_seq(self) -> int:
        self._seq += 1
        return self._seq


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def build_scenario() -> Scenario:
    rng = random.Random(SEED)
    sc = Scenario()
    for cat in CATEGORIES:
        sc.products[f"P-{cat}"] = {"id": f"P-{cat}", "name": f"{cat} item", "category": cat, "price": PRICE[cat], "source": SOURCE}

    ages: dict[str, float] = {}

    def customer(cid, name, age_days, segment="regular", region="IN-TN"):
        ages[cid] = age_days
        sc.customers.append({"id": cid, "name": name, "segment": segment, "created_at": NOW - int(age_days * DAY),
                             "region": region, "source": SOURCE, "seq": sc.next_seq()})

    def device(did, kind):
        sc.devices[did] = {"id": did, "kind": kind, "label": f"{kind} {did}", "source": SOURCE}

    def ip(iid, net):
        sc.ips[iid] = {"id": iid, "label": f"{net} {iid}", "network_type": net, "source": SOURCE}

    def session(cid, dev, ipid, start, cats, purchase=None):
        """cats: categories viewed (one event each, spaced minutes apart). purchase: (category, coupon|None)."""
        sid = f"S-{len(sc.sessions) + 1:04d}"
        sc.sessions.append({"id": sid, "customer_id": cid, "device_id": dev, "ip_id": ipid,
                            "started_at": start, "source": SOURCE, "seq": sc.next_seq()})
        t = start
        for cat in cats:
            sc._ev += 1
            sc.events.append({"id": f"E-{sc._ev:05d}", "session_id": sid, "customer_id": cid, "type": "view", "ts": t,
                              "category": cat, "product_id": f"P-{cat}", "amount": None, "source": SOURCE, "seq": sc.next_seq()})
            t += 180
        if purchase:
            cat, coupon = purchase
            sc._ev += 1
            oid = f"O-{len(sc.orders) + 1:04d}"
            amt = PRICE[cat]
            sc.events.append({"id": f"E-{sc._ev:05d}", "session_id": sid, "customer_id": cid, "type": "purchase", "ts": t,
                              "category": cat, "product_id": f"P-{cat}", "amount": amt, "source": SOURCE, "seq": sc.next_seq()})
            sc.orders.append({"id": oid, "customer_id": cid, "session_id": sid, "product_id": f"P-{cat}", "placed_at": t,
                              "amount": amt, "coupon": coupon, "source": SOURCE, "seq": sc.next_seq()})
        return sid

    def hour(day_ts, bucket):  # timestamp of that UTC day at a time inside the bucket
        base = day_ts - (day_ts % DAY)
        return base + (HOUR_BUCKET_START[bucket] + rng.randint(0, 3)) * 3600 + rng.randint(0, 1800)

    def routine(cid, dev, ipid, fav, hours, base_n=9, recent_n=4, recent_fav=None, recent_hours=None,
                coupon_rate=0.0, coupon="SPRING10"):
        """Baseline sessions spread over (account age or 120 days) .. 16 days ago; recent ones inside the last 12 days."""
        hi = min(120.0, ages[cid] - 1)
        recent_fav = recent_fav or fav
        recent_hours = recent_hours or hours
        for i in range(base_n):
            day = NOW - int((16 + ((hi - 16) * (i + rng.random() * 0.8) / base_n)) * DAY)
            cats = rng.choices(list(fav), weights=list(fav.values()), k=4)
            buy = (rng.choices(list(fav), weights=list(fav.values()))[0],
                   coupon if rng.random() < coupon_rate else None) if i % 2 == 0 else None
            session(cid, dev, ipid, hour(day, rng.choices(list(hours), weights=list(hours.values()))[0]), cats, buy)
        for i in range(recent_n):
            day = NOW - int((1 + 11 * (i + rng.random() * 0.8) / recent_n) * DAY)
            cats = rng.choices(list(recent_fav), weights=list(recent_fav.values()), k=4)
            buy = (rng.choices(list(recent_fav), weights=list(recent_fav.values()))[0], None) if i % 2 == 0 else None
            session(cid, dev, ipid, hour(day, rng.choices(list(recent_hours), weights=list(recent_hours.values()))[0]), cats, buy)

    pick2 = lambda: rng.sample(["electronics", "books", "home", "fashion", "grocery", "sports", "beauty", "toys"], 3)
    day_hours = {"morning": 3, "afternoon": 4, "evening": 3}

    # ---- 1. normal customers (16): stable behaviour, own device + residential IP
    for i in range(1, 17):
        cid = f"N{i:02d}"
        customer(cid, f"Normal {i:02d}", rng.randint(120, 600))
        device(f"DEV-{cid}", rng.choice(PRIVATE_KINDS[:3]))
        ip(f"IP-{cid}", "residential")
        a, b, c = pick2()
        routine(cid, f"DEV-{cid}", f"IP-{cid}", {a: 5, b: 3, c: 2}, day_hours, coupon_rate=0.25)
        sc.cases.append(Case(cid, 0, "normal", "stable behaviour, own device and IP"))

    # ---- 2. unusual-but-legitimate drift (4): hobby/season change, long tenure, no links
    for i, (old, new) in enumerate([({"books": 5, "home": 4}, {"sports": 5, "travel": 4}),
                                    ({"fashion": 5, "beauty": 4}, {"toys": 5, "gift_cards": 3}),
                                    ({"grocery": 5, "home": 4}, {"electronics": 5, "travel": 3}),
                                    ({"books": 6, "electronics": 3}, {"sports": 4, "fashion": 4, "travel": 2})], 1):
        cid = f"D{i:02d}"
        customer(cid, f"Legit drift {i:02d}", rng.randint(200, 500))
        device(f"DEV-{cid}", "phone")
        ip(f"IP-{cid}", "residential")
        recent_dev = f"DEV-{cid}"
        if i == 3:  # bought a new phone
            device(f"DEV-{cid}-NEW", "phone")
            recent_dev = f"DEV-{cid}-NEW"
        routine(cid, f"DEV-{cid}", f"IP-{cid}", old, day_hours, recent_fav=new, coupon_rate=0.2)
        if recent_dev != f"DEV-{cid}":  # re-home the recent sessions on the new phone
            cutoff = NOW - 14 * DAY
            for s in sc.sessions:
                if s["customer_id"] == cid and s["started_at"] > cutoff:
                    s["device_id"] = recent_dev
        sc.cases.append(Case(cid, 0, "legit_drift", "large category change but long clean history and no links"))

    # ---- 3. households sharing devices / residential IP (benign hard negatives)
    ip("IP-HH1", "residential")
    device("DEV-HH1-TV", "smart_tv")
    for i, cid in enumerate(["H01", "H02"]):
        customer(cid, f"Household 1 member {i + 1}", rng.randint(300, 700))
        device(f"DEV-{cid}", "phone")
        a, b, c = pick2()
        routine(cid, f"DEV-{cid}", "IP-HH1", {a: 5, b: 3, c: 2}, day_hours, coupon_rate=0.2)
        for k in range(3):  # shared TV sessions
            day = NOW - (30 + 25 * k + 5 * i) * DAY
            session(cid, "DEV-HH1-TV", "IP-HH1", hour(day, "evening"), [b, b, a, c])
        sc.cases.append(Case(cid, 0, "household", "shares a smart TV and home IP with a long-standing household member"))
    device("DEV-HH2-LAPTOP", "laptop")
    for i, cid in enumerate(["H03", "H04"]):
        customer(cid, f"Household 2 member {i + 1}", rng.randint(250, 650))
        device(f"DEV-{cid}", "phone")
        ip(f"IP-{cid}", "residential")
        a, b, c = pick2()
        routine(cid, f"DEV-{cid}", f"IP-{cid}", {a: 5, b: 3, c: 2}, day_hours)
        for k in range(3):
            day = NOW - (20 + 30 * k + 4 * i) * DAY
            session(cid, "DEV-HH2-LAPTOP", f"IP-{cid}", hour(day, "afternoon"), [a, c, b, a])
        sc.cases.append(Case(cid, 0, "household", "shares a family laptop with a long-standing account"))

    # ---- 4. public Wi-Fi hub: unrelated customers (incl. one newer account) on one cafe IP
    ip("IP-CAFE", "public_wifi")
    for i in range(1, 7):
        cid = f"P{i:02d}"
        customer(cid, f"Cafe visitor {i:02d}", 25 if i == 6 else rng.randint(150, 500))
        device(f"DEV-{cid}", "phone")
        ip(f"IP-{cid}", "residential")
        a, b, c = pick2()
        routine(cid, f"DEV-{cid}", f"IP-{cid}", {a: 5, b: 3, c: 2}, day_hours, base_n=9 if i < 6 else 8)
        for k in range(2):
            day = NOW - (6 + 3 * k + i) * DAY
            session(cid, f"DEV-{cid}", "IP-CAFE", hour(day, "afternoon"), [a, b, a, c])
        sc.cases.append(Case(cid, 0, "public_wifi", "uses a busy public Wi-Fi shared with unrelated people"))

    # ---- 5. library kiosk shared by three customers
    device("DEV-KIOSK", "kiosk")
    ip("IP-LIB", "corporate_nat")
    for i in range(1, 4):
        cid = f"K{i:02d}"
        customer(cid, f"Kiosk user {i:02d}", rng.randint(60, 400))
        device(f"DEV-{cid}", "phone")
        ip(f"IP-{cid}", "residential")
        a, b, c = pick2()
        routine(cid, f"DEV-{cid}", f"IP-{cid}", {a: 5, b: 3, c: 2}, day_hours, base_n=9)
        for k in range(2):
            day = NOW - (8 + 4 * k + 2 * i) * DAY
            session(cid, "DEV-KIOSK", "IP-LIB", hour(day, "morning"), [a, b, a, c])
        sc.cases.append(Case(cid, 0, "public_terminal", "occasionally uses a public library kiosk"))

    # ---- 6. RING 1 (the demo): device X shared by A1, B1 + (dormant D1 later) + benign long-term household member C1
    ring_start = NOW - 38 * DAY
    customer("C1", "Chandra (household member)", 420)
    customer("A1", "Anika Rao", 38)
    customer("B1", "Bala Menon", 37)
    customer("D1", "Dev Iyer (dormant new account)", 37)
    device("DEV-X", "phone"); device("DEV-A1", "phone"); device("DEV-B1", "phone"); device("DEV-C1", "laptop"); device("DEV-D1", "phone")
    for c in ("A1", "B1", "C1", "D1"):
        ip(f"IP-{c}", "residential")
    ip("IP-R1MOB", "mobile_carrier")
    # C1: long-standing, stable, has used DEV-X for months (family phone), no coupons
    routine("C1", "DEV-C1", "IP-C1", {"books": 5, "home": 4, "grocery": 2}, day_hours, base_n=9, recent_n=4)
    for k in range(3):
        session("C1", "DEV-X", "IP-C1", hour(NOW - (70 + 12 * k) * DAY, "evening"), ["books", "home", "books", "home"])
    sc.cases.append(Case("C1", 0, "ring1_household", "long-standing account that shares device X (benign household use)"))
    # A1 / B1: young accounts, modest history on own phone, then recent sessions on device X with the same coupon+category
    for cid, off in (("A1", 0), ("B1", 1)):
        routine(cid, f"DEV-{cid}", f"IP-{cid}", {"books": 5, "home": 3, "fashion": 2}, {"afternoon": 5, "evening": 4},
                base_n=5, recent_n=0)
        session(cid, f"DEV-{cid}", f"IP-{cid}", NOW - (6 + off) * DAY, ["books", "home", "fashion", "books"])
        session(cid, "DEV-X", "IP-R1MOB", NOW - (5 + off) * DAY + 2 * 3600, ["books", "gift_cards", "home", "gift_cards"],
                ("gift_cards", "NEWUSER50"))
        session(cid, "DEV-X", "IP-R1MOB", NOW - (3 + off) * DAY + 5 * 3600, ["gift_cards", "books", "fashion", "home"])
        session(cid, f"DEV-{cid}", f"IP-{cid}", NOW - (2 + off) * DAY, ["books", "home", "books", "fashion"])
    # D1: created in the same window, no activity yet (activity arrives as a live event in the demo)
    sc.cases.append(Case("A1", 1, "ring1", "device X + NEWUSER50 gift cards shared with B1 (D1 joins later)", ("B1",), 1))
    sc.cases.append(Case("B1", 1, "ring1", "device X + NEWUSER50 gift cards shared with A1 (D1 joins later)", ("A1",), 1))

    # ---- 7. RING 2: three new accounts, DIFFERENT devices, same mobile-carrier IP + coupon + category, synchronised timing
    ip("IP-R2MOB", "mobile_carrier")
    r2_age = 21
    for j, cid in enumerate(["E2", "F2", "G2"]):
        customer(cid, f"Ring2 {cid}", r2_age - j * 0.4)
        device(f"DEV-{cid}", "phone")
        ip(f"IP-{cid}", "residential")
        routine(cid, f"DEV-{cid}", f"IP-{cid}", {"fashion": 4, "books": 3, "home": 3}, {"afternoon": 4, "evening": 4},
                base_n=3, recent_n=0)
        session(cid, f"DEV-{cid}", f"IP-{cid}", NOW - 12 * DAY, ["fashion", "books", "home", "fashion"])
        session(cid, f"DEV-{cid}", "IP-R2MOB", NOW - (4 - j * 0.1) * DAY, ["toys", "gift_cards", "toys", "gift_cards"],
                ("gift_cards", "WELCOME20"))
        session(cid, f"DEV-{cid}", "IP-R2MOB", NOW - (3 - j * 0.1) * DAY, ["gift_cards", "toys", "gift_cards", "toys"])
        others = tuple(x for x in ("E2", "F2", "G2") if x != cid)
        sc.cases.append(Case(cid, 1, "ring2", "new accounts on one mobile-carrier IP redeeming the same coupon on the same category", others, 1))

    # ---- 8. RING 3: chain A3 -devY1- B3 -devY2- C3 (C3 only reachable in two customer hops from A3)
    device("DEV-Y1", "tablet"); device("DEV-Y2", "laptop")
    chain = ["A3", "B3", "C3"]
    for j, cid in enumerate(chain):
        customer(cid, f"Ring3 {cid}", 30 - j * 0.5)
        device(f"DEV-{cid}", "phone")
        ip(f"IP-{cid}", "residential")
        routine(cid, f"DEV-{cid}", f"IP-{cid}", {"home": 4, "fashion": 3, "books": 3}, {"afternoon": 4, "evening": 4},
                base_n=4, recent_n=0)
        session(cid, f"DEV-{cid}", f"IP-{cid}", NOW - 11 * DAY, ["home", "fashion", "books", "home"])
        session(cid, f"DEV-{cid}", f"IP-{cid}", NOW - 9 * DAY, ["fashion", "home", "books", "fashion"])
    session("A3", "DEV-Y1", "IP-A3", NOW - 6 * DAY, ["electronics", "electronics", "gift_cards", "electronics"], ("electronics", "WELCOME20"))
    session("B3", "DEV-Y1", "IP-B3", NOW - 5.5 * DAY, ["electronics", "electronics", "gift_cards", "electronics"], ("electronics", "WELCOME20"))
    session("B3", "DEV-Y2", "IP-B3", NOW - 5 * DAY, ["electronics", "electronics", "travel", "electronics"])
    session("C3", "DEV-Y2", "IP-C3", NOW - 4.5 * DAY, ["electronics", "electronics", "gift_cards", "electronics"], ("electronics", "WELCOME20"))
    session("A3", "DEV-A3", "IP-A3", NOW - 3 * DAY, ["home", "fashion", "electronics", "books"])
    session("C3", "DEV-C3", "IP-C3", NOW - 2 * DAY, ["home", "electronics", "books", "fashion"])
    sc.cases.append(Case("A3", 1, "ring3", "chain A3-Y1-B3-Y2-C3; coupon cluster only complete at two hops", ("B3", "C3"), 2))
    sc.cases.append(Case("B3", 1, "ring3", "middle of the chain", ("A3", "C3"), 1))
    sc.cases.append(Case("C3", 1, "ring3", "end of the chain; A3 is two hops away", ("A3", "B3"), 2))

    # ---- 9. decoys: popular coupons used by unrelated customers on DIFFERENT categories (no links, no cluster)
    for cid in ("N03", "N07", "N11", "N14"):
        order_ids = [o for o in sc.orders if o["customer_id"] == cid]
        if order_ids:
            order_ids[-1]["coupon"] = "NEWUSER50"
    return sc


def demo_event() -> dict:
    """The live 'new event' of the Hidden Connection Challenge: dormant account D1 appears on device X."""
    return {
        "customer_id": "D1", "session_id": "S-LIVE-D1", "device_id": "DEV-X", "ip_id": "IP-R1MOB",
        "type": "purchase", "category": "gift_cards", "product_id": "P-gift_cards", "amount": 100.0,
        "coupon": "NEWUSER50", "order_id": "O-LIVE-D1", "ts": NOW + 2 * 3600,
    }


def load_scenario(store: GraphStore, reset: bool = True) -> dict:
    sc = build_scenario()
    if reset:
        store.reset()
    store.run("meta_set_clock", {"now_ts": NOW, "scenario": SCENARIO_ID})
    store.run("upsert_customers", {"rows": sc.customers})
    store.run("upsert_devices", {"rows": list(sc.devices.values())})
    store.run("upsert_ips", {"rows": list(sc.ips.values())})
    store.run("upsert_products", {"rows": list(sc.products.values())})
    store.run("upsert_sessions", {"rows": sc.sessions})
    for i in range(0, len(sc.events), 500):
        store.run("upsert_events", {"rows": sc.events[i:i + 500]})
    store.run("upsert_orders", {"rows": sc.orders})
    coupon_rows = [{"order_id": o["id"], "coupon": o["coupon"], "source": SOURCE} for o in sc.orders if o["coupon"]]
    store.run("link_order_coupons", {"rows": coupon_rows})
    # set the sequence counter above everything loaded, so live events always get higher numbers
    with store._lock:
        store._graph.query("MERGE (m:Meta {id:'counter'}) SET m.seq = $s", {"s": sc._seq})
    return {"scenario": SCENARIO_ID, "customers": len(sc.customers), "sessions": len(sc.sessions), "events": len(sc.events),
            "orders": len(sc.orders), "cases": len(sc.cases), "positives": sum(c.label for c in sc.cases)}


def cases() -> list[Case]:
    return build_scenario().cases
