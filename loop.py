# loop.py — single-pass writer for GitHub Actions (Axiom version)
import os, time, json, math, requests, redis
from datetime import datetime, timezone

REDIS_URL = os.environ["UPSTASH_REDIS_URL"]
r = redis.Redis.from_url(REDIS_URL, decode_responses=True)

session = requests.Session()
session.headers.update({"User-Agent":"sol-hunter-feeder/1.0"})

DEX_SEARCH = "https://api.dexscreener.com/latest/dex/search?q=solana"

def norm_pair(p: dict):
    """
    Normalize so base token = the meme coin and quote = SOL.
    Return mint, symbol, liquidity_usd, main_pair_url, pair_hint.
    pair_hint is the last path segment of the Dexscreener URL.
    """
    base = p.get("baseToken") or {}
    quote = p.get("quoteToken") or {}
    b_sym = (base.get("symbol") or "").upper()
    q_sym = (quote.get("symbol") or "").upper()

    # if SOL is base and the quote is not SOL, flip them
    if b_sym == "SOL" and q_sym != "SOL":
        base, quote = quote, base
        b_sym = (base.get("symbol") or "").upper()

    mint = base.get("address")
    sym  = base.get("symbol") or "UNK"

    try:
        liq = float((p.get("liquidity") or {}).get("usd") or 0)
    except:
        liq = 0.0

    url = p.get("url") or ""
    pair_hint = url.rsplit("/", 1)[-1] if "/" in url else url

    return mint, sym, liq, url, pair_hint

def score_pair(p: dict) -> int:
    """
    Score 1..100. Higher = more interesting.
    Liquidity, 24h volume, and net 1h buy pressure.
    """
    liq = float((p.get("liquidity") or {}).get("usd") or 0)
    vol = float((p.get("volume")   or {}).get("h24") or 0)
    tx1 = (p.get("txns") or {}).get("h1") or {}
    net = (tx1.get("buys") or 0) - (tx1.get("sells") or 0)

    s_liq = math.log10(max(liq,1))*40          # liquidity weight
    s_vol = math.log10(max(vol,1))*40          # volume weight
    s_net = max(min(net,50),-50)/50*20         # buy pressure weight
    raw = s_liq + s_vol + s_net

    # clamp 1..100
    return max(1, min(100, int(round(raw))))

def fetch_pairs(limit=30):
    """
    Pull recent Solana pairs from Dexscreener.
    Keep only unique mints with nonzero liquidity.
    """
    try:
        d = session.get(DEX_SEARCH, timeout=10).json() or {}
        pairs = d.get("pairs") or []
    except:
        return []

    keep = []
    seen = set()
    for p in pairs:
        if p.get("chainId") != "solana":
            continue

        mint, sym, liq, url, pair_hint = norm_pair(p)
        if not mint:
            continue
        if mint in seen:
            continue
        if liq <= 0:
            continue

        seen.add(mint)

        sc = score_pair(p)

        # stash computed info for saving
        p["_mint"] = mint
        p["_sym"] = sym
        p["_liq"] = liq
        p["_url"] = url
        p["_pair_hint"] = pair_hint
        p["_score"] = sc

        keep.append(p)

    # sort by score desc
    keep.sort(key=lambda x: x["_score"], reverse=True)
    return keep[:limit]

def save_card(mint, sym, score, url, pair_hint, liq):
    """
    Build the card that the API and GPT read.
    Router is now Axiom, not Jupiter.
    Slippage and impact are bumped for Axiom-style manual execution.
    We also expose pair_hint so you can paste into Axiom quickly.
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
            # starter position sizing: ~0.75% of bankroll
            "position_pct": 0.0075,

            # Axiom flow often uses higher slippage to actually get filled
            # on new thin pools, and may add priority fee.
            "max_slippage_pct": 5.0,
            "expected_impact_pct": 5.0,

            # tell the GPT and you which venue to use
            "router": "Axiom",

            # give user manual reminders for how to execute in Axiom
            "execution_notes": [
                "open Axiom and paste pair_hint",
                "use priority fee if volume is spiking",
                "size with position_pct of bankroll"
            ]
        },
        "exits": {
            # take-profit ladder in % gain from entry
            "tp_levels_pct": [50, 100, 200],
            # how much to unload at each TP
            "tp_allocs_pct": [25, 50, 25],

            # invalidation cutoff. if price nukes this % below entry,
            # assume thesis is dead
            "invalidation_drop_pct": 25
        },
        "ops": {
            "pre_trade_checks": ["dust_ok"],
            "post_trade": ["journal"]
        },
        "refs": {
            # dexscreener page to inspect tape and liquidity
            "dex": url,
            # this is the pool / pair id (last path segment) so you can
            # search quickly in Axiom
            "pair_hint": pair_hint,
            # surface liquidity for fast sanity check before you ape
            "liquidity_usd": liq
        },
        # timestamp so GPT and you can tell how fresh this card is
        "asof": datetime.now(timezone.utc).isoformat()
    }

    r.set(f"card:{mint}", json.dumps(card))
    r.zadd("candidates", {mint: float(score)})

def main():
    wrote = 0
    for p in fetch_pairs(limit=30):
        save_card(
            p["_mint"],
            p["_sym"],
            p["_score"],
            p["_url"],
            p["_pair_hint"],
            p["_liq"]
        )
        wrote += 1

    # freshness beacons so /health and GPT can tell data is live
    r.set("last_update_ms", int(time.time() * 1000))
    r.incr("scan_seq")

    print(f"wrote={wrote}")

if __name__ == "__main__":
    main()
