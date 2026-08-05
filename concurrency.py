"""
Yuk boshqaruvi: single-flight qulfi va admission control.

Ikkita alohida muammoni hal qiladi — ularni chalkashtirmaslik muhim:

1. BIR XIL video, ko'p foydalanuvchi (Telegram'ga tashlangan video).
   Qulfsiz 30 kishining o'ttizi ham to'liq tarjimani qaytadan to'laydi,
   chunki tarjima keshi gap darajasida — birinchi foydalanuvchi ishni
   tugatmaguncha kesh bo'sh. `single_flight` bittasini ishlatadi,
   qolganlari tayyor natijani kutadi.

2. HAR XIL videolar, ko'p foydalanuvchi (100 kishi 100 xil video).
   Bu yerda qulf yordam bermaydi — hech qanday takrorlanish yo'q.
   Kerak bo'lgani chegara: bir vaqtda nechta og'ir ish ketishi.
   `cold_slot` shuni ushlab turadi, ortiqchasi navbatda kutadi va
   sig'masa toza "band" javobi oladi (osilib qolish o'rniga).
"""

import os
import time
import threading

from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from redis_manager import redis_client
from cache import get_cache, set_cache


# =====================================================================
# 1) ADMISSION CONTROL — bir vaqtda nechta og'ir ish
# =====================================================================
#
# Nima "og'ir": keshda yo'q sahifa — YouTube'dan transkript olish va
# OpenAI'ga tarjima so'rovlari. Bittasi 15-40 sekund thread ushlaydi.
#
# Chegara nima uchun kerak: chegara bo'lmasa 100 ta so'rov 100 ta ish
# boshlaydi, OpenAI 429 qaytaradi, kod qayta uradi, thread'lar tugaydi
# va HAMMA so'rov sekinlashadi — hatto keshdan o'qiydiganlari ham.
# Chegara bo'lsa 12 tasi normal tezlikda tugaydi, qolganlari navbatda.
MAX_COLD = int(os.getenv("MAX_CONCURRENT_COLD", "12"))

# Navbatda qancha kutish mumkin. Android'da readTimeout 120s, shuning
# uchun bundan sezilarli kam bo'lishi kerak — aks holda foydalanuvchi
# javobni ko'rmaydi, timeout'ni ko'radi.
ADMISSION_WAIT = float(os.getenv("ADMISSION_WAIT_SECONDS", "45"))

_cold_slots = threading.BoundedSemaphore(MAX_COLD)

# Faqat kuzatuv uchun — Railway logida "band" holati qanchalik tez-tez
# bo'layotganini ko'rish uchun.
_stats_lock = threading.Lock()
_stats = {"busy": 0, "waited": 0, "followers": 0}


class ServerBusy(Exception):
    """Navbat to'lgan — chaqiruvchi foydalanuvchiga toza xabar berishi kerak."""
    pass


def _bump(name):
    with _stats_lock:
        _stats[name] += 1


def load_stats():
    with _stats_lock:
        return dict(_stats, max_cold=MAX_COLD)


@contextmanager
def cold_slot():
    """Og'ir ish uchun joy oladi. Joy bo'shamasa ServerBusy otadi."""

    started = time.time()

    if not _cold_slots.acquire(timeout=ADMISSION_WAIT):
        _bump("busy")
        print("ADMISSION: navbat to'ldi, rad etildi (limit=%d)" % MAX_COLD)
        raise ServerBusy()

    waited = time.time() - started

    if waited > 0.5:
        _bump("waited")
        print("ADMISSION: navbatda %.1fs kutildi" % waited)

    try:
        yield
    finally:
        _cold_slots.release()


# =====================================================================
# 2) SINGLE FLIGHT — bir xil ishni ikki marta qilmaslik
# =====================================================================

# Qulf muddati. Yetakchi qulab tushsa qulf SHU vaqtdan keyin o'zi
# ochiladi — ya'ni hech kim abadiy kutib qolmaydi. Whisper yo'li
# (subtitri yo'q video) 1-3 daqiqa olishi mumkin, shuning uchun uzun.
LOCK_TTL = int(os.getenv("SINGLE_FLIGHT_LOCK_TTL", "180"))

# Kutuvchining chegarasi. Bu vaqtda natija kelmasa ServerBusy.
FOLLOWER_WAIT = float(os.getenv("SINGLE_FLIGHT_WAIT", "60"))

POLL_INTERVAL = 0.25


def _release(lock_key, token):
    """Faqat O'Z qulfini ochadi.

    Token tekshiruvisiz: yetakchi kechikib qulf TTL bo'yicha ochilsa va
    yangi yetakchi qulfni olgan bo'lsa, eski yetakchi tugaganda YANGI
    yetakchining qulfini ochib yuborardi.
    """
    try:
        if redis_client.get(lock_key) == token:
            redis_client.delete(lock_key)
    except Exception as error:
        print("LOCK RELEASE ERROR:", error)


def single_flight(result_key, ttl, produce, cacheable=None):
    """
    Bir xil `result_key` uchun `produce` bir vaqtda FAQAT bir marta ishlaydi.

    result_key : natija saqlanadigan Redis kaliti
    ttl        : natija necha sekund yashaydi
    produce    : og'ir ish (chaqiruvchi uni cold_slot ichiga o'rashi kerak)
    cacheable  : natijani keshlash kerakmi — vaqtinchalik xatolarni
                 uzoq muddatga yozib qo'ymaslik uchun

    Redis yo'q bo'lsa qulf ham yo'q: ish oddiy bajariladi. Ya'ni Redis
    uzilsa ilova ishlashda davom etadi, faqat qimmatroq bo'ladi.
    """

    if redis_client is None:
        return produce()

    lock_key = "lock:" + result_key
    deadline = time.time() + FOLLOWER_WAIT
    is_follower = False

    while True:

        # Yetakchi natijani yozib bo'ldimi?
        cached = get_cache(result_key)

        if cached is not None:
            if is_follower:
                print("SINGLE FLIGHT: tayyor natija olindi %s" % result_key)
            return cached

        token = uuid4().hex

        try:
            acquired = redis_client.set(
                lock_key,
                token,
                nx=True,
                ex=LOCK_TTL
            )
        except Exception as error:
            # Redis uzildi — qulfsiz davom etamiz, to'xtab qolmaymiz.
            print("LOCK ACQUIRE ERROR:", error)
            return produce()

        if acquired:

            try:
                result = produce()

                should_cache = (
                    cacheable(result) if cacheable is not None else True
                )

                if should_cache:
                    set_cache(result_key, result, ttl)

                return result

            finally:
                _release(lock_key, token)

        # Qulf boshqada. Kutamiz.
        if not is_follower:
            is_follower = True
            _bump("followers")

        if time.time() >= deadline:
            print("SINGLE FLIGHT: kutish tugadi %s" % result_key)
            raise ServerBusy()

        time.sleep(POLL_INTERVAL)


# =====================================================================
# 3) ISH THREAD'LARI
# =====================================================================
#
# asyncio ning standart executor'i min(32, cpu+4) thread beradi va u
# butun jarayon uchun umumiy. Kutayotgan follower'lar ham thread
# egallaydi, shuning uchun bu son katta bo'lishi kerak — ular deyarli
# hech narsa qilmaydi, faqat uxlaydi. Haqiqiy ishni MAX_COLD cheklaydi.
WORK_THREADS = int(os.getenv("WORK_THREADS", "160"))

work_pool = ThreadPoolExecutor(
    max_workers=WORK_THREADS,
    thread_name_prefix="lt-work"
)
