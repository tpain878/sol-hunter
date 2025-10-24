import os, time, json, redis, requests
from dotenv import load_dotenv; load_dotenv()

# --- Redis ---
r = redis.Redis.from_url(os.getenv("UPSTASH_REDIS_URL"), decode_responses=True)
NOW_MS = lambda: int(time.time() * 1000)

DEX_SEARCH = "https://api.dexscreener.com/latest/dex/search?q=SOL"

def score_from_pair(p: dict) -> int:
    """Very simple heuristic score 1..99 from liquidity + short-term move."""
    liq = ((p.get("liquidity") or {}).get("usd") or 0)          # USD liquidity
    ch1 = ((p.get("priceChange") or {}).get("h1") or 0)         # 1h %
    ch5 = ((p.get("priceChange") or {}).get("m5") or 0)         # 5m %
    raw = (liq / 12_000) + (ch1 * 0.6) + (ch5 * 0.4) + 70       # bias up a bit
    return max(1, min(99, int(raw)))

def push_card(mint: str, score: int):
    sym = (mint[:4] or "TKN").upper()
    card = {
        "token": {"symbol": sym, "mint": mint, "decimals": 9},
        "why_now": ["buyers up", "lp locked", "route stable"],
        "hard_fails": [],
        "score": {"total": score},
        "plan": {
            "position_pct": 0.0075,
            "max_slippage_pct": 1.2,
            "expected_impact_pct": 1.3,
            "router": "Jupiter",
        },
        "exits": {"tp_levels_pct": [50, 100, 200], "tp_allocs_pct": [25, 50, 25], "invalidation_drop_pct": 25},
        "ops": {"pre_trade_checks": ["dust_ok"], "post_trade": ["journal"]},
    }
    r.set(f"card:{mint}", json.dumps(card))
    r.zadd("candidates", {mint: score})

def top_sol_pairs(limit: int = 5) -> list[dict]:
    """
    Fetch trending/new pairs on Solana where quote is SOL.
    Returns a list of pair dicts (may be empty).
    """
    resp = requests.get(DEX_SEARCH, timeout=10)
    data = resp.json() if resp.ok else {}
    pairs = (data or {}).get("pairs", []) or []
    keep = []
    for p in pairs:
        qt = (p.get("quoteToken") or {}).get("symbol", "").upper()
        bt = p.get("baseToken") or {}
        if qt == "SOL" and bt.get("address"):
            keep.append(p)
        if len(keep) >= limit:
            break
    return keep

if __name__ == "__main__":
    print("loop: live feed -> Redis every 60s. Ctrl+C to stop.")
    while True:
        try:
            pushed = 0
            for pair in top_sol_pairs(limit=5):
                mint = (pair.get("baseToken") or {}).get("address")
                if not mint:
                    continue
                s = score_from_pair(pair)
                push_card(mint, s)
                pushed += 1

            # freshness markers so your GPT can verify new data arrived
            r.set("last_update_ms", NOW_MS())
            r.incr("scan_seq")

            print(f"wrote {pushed} live cards, seq={r.get('scan_seq')}")
        except Exception as e:
            # never crash the loop; just log and try again next minute
            print("loop error:", repr(e))
        time.sleep(60)
