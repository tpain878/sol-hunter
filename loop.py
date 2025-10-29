# loop.py — feeder that writes ranked candidates into Redis
# No FastAPI imports. Safe to run headless from GitHub Actions.

import os, time, json, math, requests, redis
from datetime import datetime, timezone

# ========= Tunable knobs (safe defaults) =========
WRITE_LIMIT             = 60   # how many candidates to keep per run
MIN_TVL_USD_FOR_AMM     = 0    # allow tiny AMM pools; we will still score them low
MIN_TXNS_H1_FOR_ORDERBK = 3    # require a little tape on orderbooks
REQUEST_TIMEOUT_SEC     = 10

# Known AMM vs orderbook ids (dexscreener "dexId")
AMM_DEXES        = {"raydium", "raydium-clmm", "orca", "orca-clmm", "meteora", "pumpfun", "saros"}
ORDERBOOK_DEXES  = {"phoenix", "openbook", "serum", "goosefx", "jupiter-limit"}

# ========= Redis =========
REDIS_URL = os.environ["UPSTASH_REDIS_URL"]
r = redis.Redis.from_url(REDIS_URL, decode_responses=True)

# ========= HTTP =========
session = requests.Session()
session.headers.update({"User-Agent": "sol-hunter-feeder/1.0"})

# Source of live pairs
DEX_SEARCH = "https://api.dexscreener.com/latest/dex/search?q=solana"


# -------------------- helpers --------------------
def _f(x, default=0.0):
    try:
        return float(x)
    except:
        return default


def norm_pair(p: dict):
    """
    Normalize a dexscreener pair:
      returns: mint, sym, liq_usd (float or None), dex_id, url
    We want the NON-SOL side as the meme token.
    """
    base = p.get("baseToken") or {}
    quote = p.get("quoteToken") or {}
    b_sym = (base.get("symbol") or "").upper()
    q_sym = (quote.get("symbol") or "").upper()

    # Flip so 'base' becomes the meme side when base is SOL and quote is not.
    if b_sym == "SOL" and q_sym != "SOL":
        base, quote = quote, base
        b_sym = (base.get("symbol") or "").upper()

    mint   = base.get("address")
    symbol = base.get("symbol") or "UNK"

    # AMMs supply liquidity.usd. Orderbooks often don't (None).
    liq_obj = (p.get("liquidity") or {})
    liq_usd = liq_obj.get("usd")
    liq_usd = _f(liq_usd, None) if liq_usd is not None else None

    dex_id = (p.get("dexId") or "").lower()
    url    = p.get("url")
    return mint, symbol, liq_usd, dex_id, url


def score_pair(p: dict) -> int:
    """
    Score ~ [1..100] using liquidity, 24h volume, and 1h net buys.
    Thin things score low but still can appear if the universe is thin.
    """
    liq = _f((p.get("liquidity") or {}).get("usd"))
    vol = _f((p.get("volume") or {}).get("h24"))
    tx1 = (p.get("txns") or {}).get("h1") or {}
    net = (tx1.get("buys") or 0) - (tx1.get("sells") or 0)

    s_liq = math.log10(max(liq, 1.0)) * 40.0
    s_vol = math.log10(max(vol, 1.0)) * 40.0
    s_net = max(min(net, 50), -50) / 50.0 * 20.0
    score = int(round(s_liq + s_vol + s_net))
    return max(1, min(100, score))


def fetch_pairs(limit=WRITE_LIMIT, min_tvl_usd_for_amm=MIN_TVL_USD_FOR_AMM):
    """Pull from dexscreener and apply relaxed but sane filters."""
    try:
        d = session.get(DEX_SEARCH, timeout=REQUEST_TIMEOUT_SEC).json() or {}
        pairs = d.get("pairs") or []
    except Exception:
        return []

    keep, seen = [], set()
    for p in pairs:
        if p.get("chainId") != "solana":
            continue

        mint, sym, liq, dex_id, url = norm_pair(p)
        if not mint or mint in seen:
            continue
        seen.add(mint)

        tx1   = (p.get("txns") or {}).get("h1") or {}
        buys  = tx1.get("buys") or 0
        sells = tx1.get("sells") or 0
        tape  = (buys + sells)

        is_orderbook = dex_id in ORDERBOOK_DEXES or (liq is None and dex_id not in AMM_DEXES)

        # Relaxed rules:
        if is_orderbook:
            if tape < MIN_TXNS_H1_FOR_ORDERBK:
                continue
        else:
            # AMM: allow tiny TVL (>= 0); we still score low
            if liq is None or liq < min_tvl_usd_for_amm:
                continue

        p["_mint"], p["_sym"], p["_url"] = mint, sym, url
        p["_dex_id"], p["_liq"] = dex_id, liq
        p["_score"] = score_pair(p)
        keep.append(p)

    keep.sort(key=lambda x: x["_score"], reverse=True)
    return keep[:limit]


def save_card(mint, sym, score, url, dex_id, liq_usd):
    card = {
        "token": {"symbol": sym, "mint": mint, "decimals": 9},
        "why_now": ["route stable"],
        "score": {"total": score},
        "plan": {
            "position_pct": 0.0075,
            "max_slippage_pct": 1.2,
            "expected_impact_pct": 1.3,
            "router": "Axiom-or-Jupiter",
        },
        "exits": {"tp_levels_pct": [50, 100, 200], "tp_allocs_pct": [25, 50, 25]},
        "ops": {"pre_trade_checks": ["dust_ok"], "post_trade": ["journal"]},
        "refs": {
            "dex": url,
            "dex_id": dex_id,
            "liquidity_usd": liq_usd if liq_usd is not None else "N/A",
        },
        "asof": datetime.now(timezone.utc).isoformat(),
    }
    r.set(f"card:{mint}", json.dumps(card))
    r.zadd("candidates", {mint: float(score)})


def main():
    wrote = 0
    for p in fetch_pairs():
        save_card(p["_mint"], p["_sym"], p["_score"], p["_url"], p["_dex_id"], p["_liq"])
        wrote += 1

    r.set("last_update_ms", int(time.time() * 1000))
    r.incr("scan_seq")
    print(f"wrote={wrote}")


if __name__ == "__main__":
    main()
