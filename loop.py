from fastapi import FastAPI, HTTPException
import os, json, math, requests, redis
from datetime import datetime, timezone

# --- Redis connection for /scan and /health ---
REDIS_URL = os.getenv(
    "UPSTASH_REDIS_URL",
    "rediss://default:AWDJAAIncDJiYzA2YjM4NTliYzU0NzY3OWYwNzFhNzQ3YzQ4ZTBhOXAyMjQ3Nzc@keen-ferret-24777.upstash.io:6379"
)
r = redis.Redis.from_url(REDIS_URL, decode_responses=True)

app = FastAPI()

DEX_TOKEN_ENDPOINT = "https://api.dexscreener.com/latest/dex/tokens/"


def _pick_best_pool_for_mint(mint: str):
    """
    Query Dexscreener for this mint.
    Return the single 'best' Solana pool dict for that mint:
    - chainId == solana
    - token is actually this mint
    - choose highest liquidity_usd
    Also return normalized fields we care about.
    """
    try:
        resp = requests.get(f"{DEX_TOKEN_ENDPOINT}{mint}", timeout=10)
        data = resp.json()
    except Exception:
        return None

    pairs = data.get("pairs") or []
    if not pairs:
        return None

    best = None
    best_liq = 0.0

    for p in pairs:
        # must be Solana
        if p.get("chainId") != "solana":
            continue

        base = p.get("baseToken") or {}
        quote = p.get("quoteToken") or {}

        b_sym = (base.get("symbol") or "").upper()
        q_sym = (quote.get("symbol") or "").upper()

        # normalize so meme coin is base, not SOL
        # if SOL is base and quote is not SOL, flip
        if b_sym == "SOL" and q_sym != "SOL":
            base, quote = quote, base

        # we only want pools where THIS mint is the traded coin
        if base.get("address") != mint:
            continue

        # liquidity in USD
        try:
            liq_usd = float((p.get("liquidity") or {}).get("usd") or 0)
        except Exception:
            liq_usd = 0.0

        # pick highest liquidity
        if liq_usd > best_liq:
            best_liq = liq_usd
            best = {
                "pool": p,
                "liq_usd": liq_usd,
                "symbol": base.get("symbol") or "UNK",
                "dex_url": p.get("url") or ""
            }

    return best


def _score_pool(pool_obj: dict) -> int:
    """
    Same scoring logic as loop.py:
    liquidity + volume + 1h net buy pressure → clamp 1..100
    """
    p = pool_obj["pool"]

    liq = float((p.get("liquidity") or {}).get("usd") or 0)
    vol = float((p.get("volume")   or {}).get("h24") or 0)

    tx1 = (p.get("txns") or {}).get("h1") or {}
    net = (tx1.get("buys") or 0) - (tx1.get("sells") or 0)

    s_liq = math.log10(max(liq, 1)) * 40
    s_vol = math.log10(max(vol, 1)) * 40
    s_net = max(min(net, 50), -50) / 50 * 20

    raw = s_liq + s_vol + s_net
    score_val = max(1, min(100, int(round(raw))))
    return score_val


def _build_execution_blocks(liquidity_usd: float):
    """
    Build both Axiom and Jupiter execution guidance.
    - Axiom is considered supported if liquidity_usd > 0.
    - Jupiter is considered supported if liquidity_usd >= 2000.
    Slippage assumptions:
    - Axiom: 5.0% default, manual entry, can use priority fee.
    - Jupiter: 1.2% default, assumes a more stable route.
    Position sizing stays 0.0075 (0.75% bankroll).
    """
    axiom_block = {
        "router": "Axiom",
        "supported": liquidity_usd > 0,
        "slippage_pct": 5.0,
        "position_pct": 0.0075,
        "instructions": [
            "Open Axiom",
            "Search for pair_hint",
            "Set slippage to 5.0%",
            "Size ~0.75% of bankroll (position_pct)",
            "Add priority fee if volume is spiking"
        ]
    }

    jupiter_block = {
        "router": "Jupiter",
        "supported": liquidity_usd >= 2000,
        "slippage_pct": 1.2,
        "position_pct": 0.0075,
        "instructions": [
            "Open Jupiter",
            "Paste the mint",
            "Check that route is normal (no insane slippage)",
            "If slippage > ~2-3% or route looks broken, do not trade here"
        ]
    }

    return {
        "axiom": axiom_block,
        "jupiter": jupiter_block
    }


@app.get("/health")
def health():
    """
    Basic health plus feeder freshness.
    """
    seq = r.get("scan_seq")
    last = r.get("last_update_ms")

    return {
        "env": {
            "HELIUS_API_KEY": bool(os.getenv("HELIUS_API_KEY")),
            "UPSTASH_REDIS_URL": bool(os.getenv("UPSTASH_REDIS_URL") or REDIS_URL),
        },
        "scan_seq": int(seq) if seq else 0,
        "last_update_ms": int(last) if last else 0,
    }


@app.get("/scan")
def scan(limit: int = 10):
    """
    Ranked list from Redis.
    This is still how you get top N plays from the feeder loop.
    """
    mints = r.zrevrange("candidates", 0, limit - 1)

    items = []
    for m in mints:
        # skip obviously fake/test keys
        if m.startswith("DUMMY"):
            continue

        raw = r.get(f"card:{m}")
        if not raw:
            continue

        try:
            card = json.loads(raw)
        except json.JSONDecodeError:
            continue

        items.append(card)

    return items


@app.get("/evaluate")
def evaluate(mint: str):
    """
    Independent live evaluation for ANY mint.
    Does NOT depend on Redis or /scan.
    Steps:
    - fetch best Solana pool from Dexscreener
    - compute score
    - build dual execution block (Axiom + Jupiter)
    - attach exits and risk info
    If no usable pool is found, return 404.
    """

    pool_obj = _pick_best_pool_for_mint(mint)
    if pool_obj is None:
        raise HTTPException(status_code=404, detail="mint not found or not tradeable on solana")

    liq_usd = float(pool_obj["liq_usd"])
    symbol = pool_obj["symbol"]
    dex_url = pool_obj["dex_url"]
    pair_hint = dex_url.rsplit("/", 1)[-1] if "/" in dex_url else dex_url

    # score
    score_val = _score_pool(pool_obj)

    # risk flags
    risk_notes = []
    if liq_usd < 1000:
        risk_notes.append("THIN_POOL_HIGH_RUG_RISK")

    # execution blocks (Axiom + Jupiter)
    exec_blocks = _build_execution_blocks(liq_usd)

    # exits / plan structure for GPT
    card = {
        "token": {
            "symbol": symbol,
            "mint": mint,
            "decimals": 9
        },
        "score": {
            "total": score_val
        },
        "market": {
            "liquidity_usd": liq_usd,
            "dex_url": dex_url,
            "pair_hint": pair_hint,
            "risk": risk_notes,
            "asof": datetime.now(timezone.utc).isoformat()
        },
        "execution": exec_blocks,
        "exits": {
            "tp_levels_pct": [50, 100, 200],
            "tp_allocs_pct": [25, 50, 25],
            "invalidation_drop_pct": 25
        }
    }

    return card
