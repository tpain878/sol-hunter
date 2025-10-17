from fastapi import FastAPI
import os, json, redis

app = FastAPI()
r = redis.Redis.from_url(os.getenv("UPSTASH_REDIS_URL"), decode_responses=True)

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
