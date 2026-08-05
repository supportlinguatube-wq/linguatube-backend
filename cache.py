import json
import os

from redis_manager import redis_client

# Har bir yozuvda ikki qator log chiqardi va Railway logini bosib ketardi.
# Kerak bo'lsa CACHE_DEBUG=1 qo'yib qaytarasiz.
CACHE_DEBUG = os.getenv("CACHE_DEBUG") in ("1", "true", "yes", "on")

TRANSCRIPT_TTL = 60 * 60 * 24 * 7

# Tayyor, tarjima qilingan SAHIFA. Xom transkriptdan farqi shu: uni
# qaytarish uchun hech narsa hisoblash kerak emas.
#
# Muddati qisqaroq (3 kun), chunki bular eng katta yozuvlar. Muddati
# o'tsa ham qayta qurish arzon — har bir gap alohida 30 kun keshda.
PAGE_TTL = 60 * 60 * 24 * 3
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

        # json.dumps ikki marta chaqirilardi — endi bir marta
        data = json.dumps(value)

        if CACHE_DEBUG:
            size = len(data.encode("utf-8"))

            
            # print("REDIS SAVE %.1f KB  %s" % (size / 1024.0, key))

        redis_client.setex(
            key,
            ttl,
            data
        )

    except Exception as error:

        print("REDIS SET ERROR:", error)