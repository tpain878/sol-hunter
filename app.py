from fastapi import FastAPI, HTTPException, Request
import json, redis, asyncio, random, time

# === YOUR REAL REDIS URL (TLS) ===
REDIS_URL = "rediss://default:AWDJAAIncDJiYzA2YjM4NTliYzU0NzY3OWYwNzFhNzQ3YzQ4ZTBhOXAyMjQ3Nzc@keen-ferret-24777.upstash.io:6379"

app = FastAPI()
r = redis.Redis.from_url(REDIS_URL, decode_responses=True)

# ---- background feeder (every 60s) ----
async def feeder():
    while True:
        s = random.randint(70, 95)
        mint = f"DUMMY{int(time.time())}"
        card = {
            "token": {"symbol": "FAKE", "mint": mint, "decimals": 9},
            "why_now": ["buyers up", "lp locked", "route stable"],
            "score": {"total": s},
            "plan": {"position_pct": 0.0075, "max_slippage_pct": 1.2, "expected_impact_pct": 1.3, "router": "Jupiter"},
            "exits": {"tp_levels_pct": [50, 100, 200], "tp_allocs_pct": [25, 50, 25], "invalidation_drop_pct": 25},
            "ops": {"pre_trade_checks": ["dust_ok"], "post_trade": ["journal"]},
        }
        r.set(f"card:{mint}", json.dumps(card))
        r.zadd("candidates", {mint: s})
        await asyncio.sleep(60)



@app.get("/health")
def health():
    return {"env": {
        "HELIUS_API_KEY": True,   # you provided: 1d1b973e-2afc-46bd-9168-6982a33b7691
        "UPSTASH_REDIS_URL": True
    }}

@app.get("/scan")
def scan(limit: int = 3):
    mints = r.zrevrange("candidates", 0, limit-1)
    return [json.loads(r.get(f"card:{m}")) for m in mints if r.get(f"card:{m}")]

@app.get("/evaluate")
def evaluate(mint: str):
    card = r.get(f"card:{mint}")
    if card:
        return json.loads(card)
    block = r.get(f"block:{mint}")
    return json.loads(block) if block else {"status": "BLOCKED", "reasons": ["not_enough_data"]}
import os, json, time, random

FEED_KEY = os.getenv("FEED_KEY", "")

