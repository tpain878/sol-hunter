import os, time, json, redis, random
from dotenv import load_dotenv; load_dotenv()

r = redis.Redis.from_url(os.getenv("UPSTASH_REDIS_URL"), decode_responses=True)

def fake_card(mint):
    score = random.randint(70,85)
    card = {
      "token":{"symbol":"FAKE","mint":mint,"decimals":9},
      "why_now":["buyers up","lp locked","route stable"],
      "hard_fails":[],
      "score":{"total":score},
      "plan":{"position_pct":0.0075,"max_slippage_pct":1.2,"expected_impact_pct":1.3,"router":"Jupiter"},
      "exits":{"tp_levels_pct":[50,100,200],"tp_allocs_pct":[25,50,25],"invalidation_drop_pct":25},
      "ops":{"pre_trade_checks":["dust_ok"],"post_trade":["journal"]}
    }
    r.set(f"card:{mint}", json.dumps(card))
    r.zadd("candidates",{mint:score})

if __name__=='__main__':
    while True:
        fake_card(f'DUMMY{int(time.time())}')
        time.sleep(60)
