from fastapi import FastAPI
import os, json, redis, asyncio, random, time

app = FastAPI()
r = redis.Redis.from_url(os.getenv("UPSTASH_REDIS_URL"), decode_responses=True)

# ---- background feeder (runs every 60s) ----
async def feeder():
    while True:
        s = random.randint(70, 95)
        mint = f"DUMMY{int(time.time())}"
        card = {"token":{"symbol":"FAKE","mint":mint,"decimals":9},
                "why_now":["buyers up","lp locked","route stable"],
                "score":{"total":s}}
        r.set(f"card:{mint}", json.dumps(card))
        r.zadd("candidates", {mint: s})
        await asyncio.sleep(60)

@app.on_event("startup")
async def start_feeder():
    asyncio.create_task(feeder())

# ---- API ----
@app.get("/health")
def health():
    return {"env":{
        "HELIUS_API_KEY": bool(os.getenv("HELIUS_API_KEY")),
        "UPSTASH_REDIS_URL": bool(os.getenv("UPSTASH_REDIS_URL")),
        "UPSTASH_REDIS_TOKEN": bool(os.getenv("UPSTASH_REDIS_TOKEN"))
    }}

@app.get("/scan")
def scan(limit: int = 3):
    mints = r.zrevrange("candidates", 0, limit-1)
    return [json.loads(r.get(f"card:{m}")) for m in mints if r.get(f"card:{m}")]

@app.get("/evaluate")
def evaluate(mint: str):
    card = r.get(f"card:{mint}")
    if card: return json.loads(card)
    block = r.get(f"block:{mint}")
    return json.loads(block) if block else {"status":"BLOCKED","reasons":["not_enough_data"]}
