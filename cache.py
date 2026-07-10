import json

from redis_manager import redis_client

TRANSCRIPT_TTL = 60 * 60 * 24 * 7
TRANSLATION_TTL = 60 * 60 * 24 * 30
VIDEO_URL_TTL = 60 * 60 * 6
WORD_TTL = 60 * 60 * 24 * 30


def get_cache(key):

    if redis_client is None:
        return None

    try:

        value = redis_client.get(key)

        if value is None:
            return None

        return json.loads(value)

    except Exception as error:

        print(error)

        return None


def set_cache(
    key,
    value,
    ttl
):

    if redis_client is None:
        return

    try:


        data = json.dumps(value)

        size = len(data.encode("utf-8"))

        print("REDIS SAVE SIZE:", size / 1024 / 1024, "MB")

        print("REDIS KEY:", key)
        redis_client.setex(
            key,
            ttl,
            json.dumps(value)
        )

    except Exception as error:

        print(error)