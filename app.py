from fastapi import FastAPI
import os, json, redis

# --- Redis connection ---
# We try env var first (Render should have UPSTASH_REDIS_URL set).
# If it's not set in env, we fall back to your known URL.
REDIS_URL = os.getenv(
    "UPSTASH_REDIS_URL",
    "rediss://default:AWDJAAIncDJiYzA2YjM4NTliYzU0NzY3OWYwNzFhNzQ3YzQ4ZTBhOXAyMjQ3Nzc@keen-ferret-24777.upstash.io:6379"
)

r = redis.Redis.from_url(REDIS_URL, decode_responses=True)

app = FastAPI()


@app.get("/health")
def health():
    """
    Basic health check + freshness check.
    scan_seq and last_update_ms are written by loop.py
    so you know if the feeder is still updating Redis.
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
    Return up to `limit` best candidates ranked by score.
    We pull the top mints from the sorted set 'candidates'
    then load each 'card:{mint}'.

    We skip anything that looks like old test data ('DUMMY...').
    """
    mints = r.zrevrange("candidates", 0, limit - 1)

    items = []
    for m in mints:
        # ignore leftover fake/test keys
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
    Return the full card for a specific mint.
    If it's been blocked (for example, flagged as bad),
    return the block info.
    """
    raw = r.get(f"card:{mint}")
    if raw:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass

    block = r.get(f"block:{mint}")
    if block:
        try:
            return json.loads(block)
        except json.JSONDecodeError:
            pass

    return {"status": "BLOCKED", "reasons": ["not_enough_data"]}
