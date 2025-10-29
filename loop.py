# loop.py — single-pass writer for GitHub Actions
import os, time, json, math, requests, redis
from datetime import datetime, timezone

REDIS_URL = os.environ["UPSTASH_REDIS_URL"]
r = redis.Redis.from_url(REDIS_URL, decode_responses=True)

session = requests.Session()
session.headers.update({"User-Agent":"sol-hunter-feeder/1.0"})

DEX_SEARCH = "https://api.dexscreener.com/latest/dex/search?q=solana"

def norm_pair(p: dict):
    base = p.get("baseToken") or {}
    quote = p.get("quoteToken") or {}

    b_sym = (base.get("symbol") or "").upper()
    q_sym = (quote.get("symbol") or "").upper()

    # make sure base token is the non-SOL side so we store THAT mint
    if b_sym == "SOL" and q_sym != "SOL":
        base, quote = quote, base
        b_sym = (base.get("symbol") or "").upper()

    mint = base.get("address")
    sym  = base.get("symbol") or "UNK"

    try:
        liq = float((p.get("liquidity") or {}).get("usd") or 0)
    except:
        liq = 0.0

    return mint, sym, liq, p.get("url")

def score_pair(p: dict) -> int:
    liq = float((p.get("liquidity") or {}).get("usd") or 0)
    vol = float((p.get("volume") or {}).get("h24") or 0)

    tx1 = (p.get("txns") or {}).get("h1") or {}
    net = (tx1.get("buys") or 0) - (tx1.get("sells") or 0)

    s_liq = math.log10(max(liq,1))*40
    s_vol = math.log10(max(vol,1))*40
    s_net = max(min(net,50),-50)/50*20

    return max(1, min(100, int(round(s_liq+s_vol+s_net))))

def fetch_pairs(limit=30):
    try:
        d = session.get(DEX_SEARCH, timeout=10).json() or {}
        pairs = d.get("pairs") or []
    except:
        return []

    keep, seen = [], set()

    for p in pairs:
        if p.get("chainId") != "solana":
            continue

        mint, sym, liq, url = norm_pair(p)
        if not mint or mint in seen or liq <= 0:
            continue

        seen.add(mint)
        p["_mint"]  = mint
        p["_sym"]   = sym
        p["_url"]   = url
        p["_score"] = score_pair(p)

        keep.append(p)

    keep.sort(key=lambda x:x["_score"], reverse=True)
    return keep[:limit]

def save_card(mint, sym, score, url):
    card = {
      "token": {
        "symbol": sym,
        "mint": mint,
        "decimals": 9
      },
      "why_now": ["route stable"],
      "score": {"total": score},
      "plan": {
        "position_pct": 0.0075,
        "max_slippage_pct": 1.2,
        "expected_impact_pct": 1.3,
        "router": "Jupiter"   # still just a label
      },
      "exits": {
        "tp_levels_pct": [50,100,200],
        "tp_allocs_pct": [25,50,25],
        "invalidation_drop_pct": 25
      },
      "ops": {
        "pre_trade_checks": ["dust_ok"],
        "post_trade": ["journal"]
      },
      "refs": {
        "dex": url
      },
      "asof": datetime.now(timezone.utc).isoformat()
    }

    # write to Redis
    r.set(f"card:{mint}", json.dumps(card))
    r.zadd("candidates", {mint: float(score)})

def main():
    wrote = 0
    for p in fetch_pairs(limit=30):
        save_card(p["_mint"], p["_sym"], p["_score"], p["_url"])
        wrote += 1

    # freshness markers
    r.set("last_update_ms", int(time.time()*1000))
    r.incr("scan_seq")
    print(f"wrote={wrote}")

if __name__ == "__main__":
    main()
