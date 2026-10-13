import sys, json
sys.path.insert(0, "audit"); sys.path.insert(0, "src")
from matrix_graph import run, summarize, BASE
out, calls, err = run({})
mem = out.get("memory")
print("memory keys after mock backfill:", list(mem or {}), "bytes", len(json.dumps(mem or {})))
def case(label, extra):
    out2, calls2, err2 = run({"request": "review_material", **extra}, memory=mem)
    body = ""
    if out2:
        r = out2.get("result") or {}
        body = {k: r.get(k) for k in ("mode", "status", "period", "period_from", "period_to") if k in r}
        txt = json.dumps(out2.get("corpus"), default=str)
        import re
        m = re.findall(r"(?:Period|period)[^\"]{0,80}", txt)[:2]
        body["period_text"] = m
    print(f"- {label:40} err={str(err2)[:120]} calls={len(calls2)} {json.dumps(body, default=str)[:300]}")
case("valid 2026-04-01..2026-09-30", {"period_from": "2026-04-01", "period_to": "2026-09-30"})
case("reversed from>to", {"period_from": "2026-09-30", "period_to": "2026-04-01"})
case("malformed 2026-13-45", {"period_from": "2026-13-45", "period_to": "2026-09-30"})
case("garbage 'last spring'", {"period_from": "last spring", "period_to": "x"})
case("only from", {"period_from": "2026-04-01"})
case("only to", {"period_to": "2026-09-30"})
case("neither", {})
case("future 2030-01-01..2030-12-31", {"period_from": "2030-01-01", "period_to": "2030-12-31"})
case("dd/mm/yyyy", {"period_from": "01/04/2026", "period_to": "30/09/2026"})
case("ask words + no dates", {"request": "weekly_update", "ask": "review material April to September"})
