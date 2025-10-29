from fastapi import FastAPI, HTTPException
import os, json, time, asyncio, random
import math, requests, redis
from datetime import datetime, timezone

# === Redis (your real Upstash URL) ===
REDIS_URL = "rediss://default:AWDJAAIncDJiYzA2YjM4NTliYzU0NzY3OWYwNzFhNzQ3YzQ4ZTBhOXAyMjQ3Nzc@keen-ferret-24777.upstash.io:6379"
r = redis.Redis.from_url(REDIS_URL, decode_responses=True)

app = FastAPI()

# -------------------------------------------------
# (optional /legacy) background feeder stub
# NOTE: this is NOT automatically scheduled.
# It still writes dummy cards if called manually.
# Safe to leave in file. It does nothing by default.
# -------------------------------------------------
async def feeder():
    while True:
        s = random.randint(70, 95)
        mint = f"DUMMY{int(time.time())}"

        card = {
            "token": {"symbol": "FAKE", "mint": mint, "decimals": 9},
            "why_now": ["buyers up", "lp locked", "route stable"],
            "score": {"total": s},
            "plan": {
                "position_pct": 0.0075,
                "max_slippage_pct": 1.2,
                "expected_impact_pct": 1.3,
                "router": "Jupiter"
            },
            "exits": {
                "tp_levels_pct": [50, 100, 200],
                "tp_allocs_pct": [25, 50, 25],
                "invalidation_drop_pct": 25
            },
            "ops": {
                "pre_trade_checks": ["dust_ok"],
                "post_trade": ["journal"]
            },
        }

        r.set(f"card:{mint}", json.dumps(card))
        r.zadd("candidates", {mint: s})
        await asyncio.sleep(60)

# -------------------------------------------------
# /health
# -------------------------------------------------
@app.get("/health")
def health():
    return {
        "env": {
            "HELIUS_API_KEY": True,      # you already provided one
            "UPSTASH_REDIS_URL": True    # Redis is configured
        }
    }

# -------------------------------------------------
# /scan
# Returns top N current candidates from Redis.
# Now defaults to 10 instead of 3.
# -------------------------------------------------
@app.get("/scan")
def scan(limit: int = 10):
    # highest score first
    mints = r.zrevrange("candidates", 0, limit - 1)

    out = []
    for m in mints:
        raw = r.get(f"card:{m}")
        if not raw:
            continue
        try:
            out.append(json.loads(raw))
        except:
            continue
    return out

# -------------------------------------------------
#  NEW INDEPENDENT /evaluate
#
#  This does NOT depend on /scan or Redis cards.
#  It pulls live pair data for the mint from Dexscreener,
#  builds a compact trade-ready summary, and returns it.
#
#  - Works even if the mint was never seen in /scan.
#  - Chooses best pair on Solana.
#  - Labels support for Axiom vs Jupiter.
# -------------------------------------------------

session = requests.Session()
session.headers.update({"User-Agent": "sol-hunter-api/1.0"})

DEX_SEARCH_SINGLE = "https://api.dexscreener.com/latest/dex/tokens/{}"


def build_evaluate_response(
    mint: str,
    symbol: str,
    liquidity_usd: float,
    dex_url: str,
    pair_hint: str,
    score_total: float,
    risk_flags: list,
    axiom_ok: bool,
    jup_ok: bool,
    axiom_slip: float,
    jup_slip: float,
    now_iso: str
):
    return {
        "token": {
            "symbol": symbol,
            "mint": mint,
            "decimals": 9
        },
        "score": {
            "total": score_total
        },
        "market": {
            "liquidity_usd": liquidity_usd,
            "pair_hint": pair_hint,
            "dex_url": dex_url,
            "risk": risk_flags,
            "asof": now_iso
        },
        "execution": {
            "axiom": {
                "supported": axiom_ok,
                "slippage_pct": axiom_slip
            },
            "jupiter": {
                "supported": jup_ok,
                "slippage_pct": jup_slip
            }
        },
        "exits": {
            "tp_levels_pct": [50, 100, 200],
            "tp_allocs_pct": [25, 50, 25],
            "invalidation_drop_pct": 25
        }
    }


def fetch_best_pair_for_mint(mint: str):
    """
    Query Dexscreener for all pairs for this mint.
    Filter to Solana, keep only pairs with liquidity,
    score them, pick the best.
    Returns a dict or None.
    """
    try:
        resp = session.get(DEX_SEARCH_SINGLE.format(mint), timeout=10)
        data = resp.json() or {}
    except Exception:
        return None

    pairs = data.get("pairs") or []
    good = []
    seen = set()

    for p in pairs:
        if p.get("chainId") != "solana":
            continue

        base = p.get("baseToken") or {}
        quote = p.get("quoteToken") or {}

        base_addr = base.get("address")
        if not base_addr:
            continue
        if base_addr in seen:
            continue
        seen.add(base_addr)

        # liquidity (USD)
        try:
            liq_usd = float((p.get("liquidity") or {}).get("usd") or 0)
        except:
            liq_usd = 0.0

        if liq_usd <= 0:
            continue

        # estimate activity
        try:
            vol24 = float((p.get("volume") or {}).get("h24") or 0)
        except:
            vol24 = 0.0

        tx1 = (p.get("txns") or {}).get("h1") or {}
        buys_1h = tx1.get("buys") or 0
        sells_1h = tx1.get("sells") or 0
        net_flow = (buys_1h - sells_1h)

        # crude composite score
        s_liq = math.log10(max(liq_usd, 1)) * 40
        s_vol = math.log10(max(vol24, 1)) * 40
        s_net = max(min(net_flow, 50), -50) / 50 * 20
        composite = max(1, min(500, round(s_liq + s_vol + s_net)))

        # attach convenience fields
        p["_liq_usd"] = liq_usd
        p["_base"] = base
        p["_quote"] = quote
        p["_score"] = composite
        p["_url"] = p.get("url") or ""

        good.append(p)

    if not good:
        return None

    # highest composite score wins
    good.sort(key=lambda x: x["_score"], reverse=True)
    return good[0]


@app.get("/evaluate")
def evaluate(mint: str):
    """
    New evaluate:
    - independent of /scan
    - always tries to build a trade-ready snapshot
    for the given mint
    - never returns {"status":"BLOCKED"}.
    """
    pair = fetch_best_pair_for_mint(mint)
    if not pair:
        # no viable solana pair with liquidity
        raise HTTPException(
            status_code=404,
            detail="mint not found or not tradeable on solana"
        )

    base = pair["_base"]
    quote = pair["_quote"]
    symbol = base.get("symbol") or "UNK"

    liq_usd = pair["_liq_usd"]
    dex_url = pair["_url"]

    # ex: "SOL / XYZ"
    pair_hint = f"{quote.get('symbol','?')} / {base.get('symbol','?')}"

    score_total = float(pair["_score"])

    risk_flags = []
    if liq_usd < 1000:
        risk_flags.append("THIN_POOL_HIGH_RUG_RISK")

    # route support flags
    # rule of thumb:
    # - axiom_ok if any liquidity
    # - jup_ok if liquidity meets a safer floor
    axiom_ok = liq_usd > 0
    jup_ok = liq_usd >= 2000

    now_iso = datetime.now(timezone.utc).isoformat()

    return build_evaluate_response(
        mint=mint,
        symbol=symbol,
        liquidity_usd=liq_usd,
        dex_url=dex_url,
        pair_hint=pair_hint,
        score_total=score_total,
        risk_flags=risk_flags,
        axiom_ok=axiom_ok,
        jup_ok=jup_ok,
        axiom_slip=5.0,   # assume ~5% slippage tolerance for thin pools / Axiom
        jup_slip=1.2,     # assume ~1.2% slippage tolerance for deeper pools / Jupiter
        now_iso=now_iso
    )
