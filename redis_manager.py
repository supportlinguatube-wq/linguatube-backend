import os
import redis

REDIS_URL = os.getenv("REDIS_URL")

redis_client = None

try:
    if REDIS_URL:
        redis_client = redis.from_url(
            REDIS_URL,
            decode_responses=True,
            health_check_interval=30
        )

        redis_client.ping()
        print("REDIS CONNECTED")

    else:
        print("REDIS URL NOT FOUND")

except Exception as error:

    print("REDIS CONNECTION ERROR:", error)
    redis_client = None