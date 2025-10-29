# loop.py — single-pass writer for GitHub Actions
#
# Purpose:
# - Pull fresh Solana meme pairs from dexscreener
# - Score them
# - Push top N into Redis sorted set "candidates"
# - Save per-mint cards under "card:{mint}"
# - Stamp last_update_ms and scan_seq so the API/GPT knows it's fresh
#
# No FastAPI imports. No Jupiter router text. This is safe to run headless.

import os, time, json, math, requests, redis
from datetime import datetime, timezone

# --- Redis connection from secret env var on GitHub Actions ---
REDIS_URL = os.environ["UPSTASH_REDIS_URL"]
r = redis.Redis.from_url(REDIS_URL, decode_responses=True)

# --- simple requests session so we look like a normal client ---
session = requests.Session()
session.headers.update({"User-Agent": "sol-hunter-feeder/1.0"})

# --- source of live pairs ---
# dexscreener gives us recent Solana pairs,
# including baseToken / quoteToken / liquidity / volume / txns, etc.
DEX_SEARCH = "https://api.dexscreener.com/latest/dex/search?q=solana"

def norm_pair(p: dict):
    """
    Normalize a dexscreener pair into:
      - mint  (the token we actually care about)
      - sym   (symbol for that token)
      - liq   (usd liquidity float)
      - url   (dexscreener url for that pair)

    We try to treat the NON-SOL side as the actual meme coin.
    If baseToken is SOL and quoteToken is not SOL, we flip so that
    'base' becomes the new thing we're evaluating.
    """
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
    """
    Score a pair 1..100 using liquidity, 24h volume, and recent buy/sell delta.
    Higher = more tradeable. This is what we sort by.
    """
    liq = float((p.get("liquidity") or {}).get("usd") or 0)
    vol = float((p.get("volume") or {}).get("h24") or 0)

    tx1 = (p.get("txns") or {}).get("h1") or {}
    net = (tx1.get("buys") or 0) - (tx1.get("sells") or 0)

    # liquidity and volume are log-scaled to compress crazy outliers
    s_liq = math.log10(max(liq, 1)) * 40
    s_vol = math.log10(max(vol, 1)) * 40

    # net buy pressure in last hour up to +/-20 pts
    s_net = max(min(net, 50), -50) / 50 * 20

    raw_score = s_liq + s_vol + s_net
    # clamp to [1,100]
    return max(1, min(100, int(round(raw_score))))

def fetch_pairs(limit=30):
    """
    Pull recent Solana pairs from dexscreener, normalize each pair,
    compute score, keep best 'limit' unique mints with liquidity>0.
    """
    try:
        d = session.get(DEX_SEARCH, timeout=10).json() or {}
        pairs = d.get("pairs") or []
    except Exception:
        return []

    keep = []
    seen = set()

    for p in pairs:
        # only look at Solana
        if p.get("chainId") != "solana":
            continue

        mint, sym, liq, url = norm_pair(p)

        # basic sanity checks
        if not mint:
            continue
        if mint in seen:
            continue
        if liq <= 0:
            continue  # skip zero-liq trash

        seen.add(mint)

        # attach helper fields to the raw record
        p["_mint"]  = mint
        p["_sym"]   = sym
        p["_url"]   = url
        p["_score"] = score_pair(p)

        keep.append(p)

    # sort high score first
    keep.sort(key=lambda x: x["_score"], reverse=True)

    # return top N
    return keep[:limit]

def save_card(mint, sym, score, url):
    """
    Write:
      - card:{mint} => full JSON card (trade plan, refs, timestamp)
      - zadd candidates => sorted set by score (descending)
    """
    card = {
        "token": {
            "symbol": sym,
            "mint": mint,
            "decimals": 9
        },
        "why_now": [
            "route stable"
        ],
        "score": {
            "total": score
        },
        "plan": {
            # how much of bankroll to risk on first entry
            "position_pct": 0.0075,
            # how much price impact / slippage we tolerate
            "max_slippage_pct": 1.2,
            # approximate impact tolerance
            "expected_impact_pct": 1.3,
            # downstream router. we are migrating execution to Axiom
            # but we still tell the GPT what route was assumed
            "router": "Axiom-or-Jupiter"
        },
        "exits": {
            # simple tiered TP ladder (take-profit chunks)
            "tp_levels_pct": [50, 100, 200],
            "tp_allocs_pct": [25, 50, 25]
        },
        "ops": {
            "pre_trade_checks": ["dust_ok"],
            "post_trade": ["journal"]
        },
        "refs": {
            # give GPT / trader a direct link to live pair
            "dex": url
        },
        # timestamp so GPT can tell if data is stale
        "asof": datetime.now(timezone.utc).isoformat()
    }

    # write per-mint card
    r.set(f"card:{mint}", json.dumps(card))

    # score in sorted set for scan()
    r.zadd("candidates", {mint: float(score)})

def main():
    """
    Main entry for GitHub Action:
    - fetch fresh pairs
    - write each to redis
    - update freshness markers
    - print wrote=<n> so the workflow logs show success
    """
    wrote = 0
    for p in fetch_pairs(limit=30):
        save_card(
            p["_mint"],
            p["_sym"],
            p["_score"],
            p["_url"]
        )
        wrote += 1

    # freshness markers for API/GPT
    r.set("last_update_ms", int(time.time() * 1000))
    r.incr("scan_seq")

    print(f"wrote={wrote}")

if __name__ == "__main__":
    main()
