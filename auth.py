"""
auth.py — Firebase propusk (ID token) tekshiruvi va balans nazorati
====================================================================

MUAMMO: /transcript endpoint'i hammaga ochiq edi. Ilova ichida backend
manzili ochiq yozilgani uchun uni topgan har kim shunday qila olardi:

    curl "https://.../transcript/VIDEO_ID"

va OpenAI hisobingizdan bepul tarjima olardi.

YECHIM: ilova har so'rovda Firebase ID token yuboradi, backend uni
tekshiradi va foydalanuvchi balansini Firestore'dan o'qiydi.

ROLLOUT — MUHIM:
    REQUIRE_AUTH default O'CHIRILGAN.
    Sababi: App Store'dagi hozirgi ilova token YUBORMAYDI. Bugun majburiy
    qilsak, barcha mavjud foydalanuvchilar darhol ishlamay qoladi.

    Tartib:
      1. Shu kodni deploy qilasiz. Hech kim buzilmaydi, faqat log yig'iladi.
      2. Log'da "AUTH: token bor" va "AUTH: token yo'q" nisbatini kuzatasiz.
      3. Ilovaning yangi versiyasi tarqalgach REQUIRE_AUTH=1 qo'yasiz.
      4. Keyin REQUIRE_BALANCE=1.
"""

import json
import os
import time

from fastapi import HTTPException

REQUIRE_AUTH = os.getenv("REQUIRE_AUTH") in ("1", "true", "yes", "on")
REQUIRE_BALANCE = os.getenv("REQUIRE_BALANCE") in ("1", "true", "yes", "on")

# Balansni har so'rovda Firestore'dan o'qish sekin. Qisqa muddat keshlaymiz.
BALANCE_TTL = int(os.getenv("BALANCE_CACHE_SECONDS", "30"))

_app = None
_init_failed = False

# uid -> (balans, o'qilgan_vaqt)
_balance_cache = {}


def _firebase():
    """Firebase Admin'ni bir marta ishga tushiradi."""
    global _app, _init_failed

    if _app is not None:
        return _app

    if _init_failed:
        return None

    raw = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON")

    if not raw:
        print("AUTH: FIREBASE_SERVICE_ACCOUNT_JSON yo'q — tekshiruv o'chiq")
        _init_failed = True
        return None

    try:
        import firebase_admin
        from firebase_admin import credentials

        cred = credentials.Certificate(json.loads(raw))
        _app = firebase_admin.initialize_app(cred)

        print("AUTH: Firebase Admin tayyor")
        return _app

    except Exception as error:
        print("AUTH INIT ERROR:", error)
        _init_failed = True
        return None


def _extract_token(authorization):
    if not authorization:
        return None

    parts = authorization.split()

    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1]

    return None


def verify_uid(authorization):
    """Token to'g'ri bo'lsa uid qaytaradi, aks holda None."""
    token = _extract_token(authorization)

    if not token:
        return None

    if _firebase() is None:
        return None

    try:
        from firebase_admin import auth as fb_auth

        decoded = fb_auth.verify_id_token(token)
        return decoded.get("uid")

    except Exception as error:
        print("AUTH: token yaroqsiz —", error)
        return None


def get_balance_seconds(uid):
    """Firestore'dagi users/{uid}.remainingSeconds. Topilmasa None."""
    if not uid or _firebase() is None:
        return None

    cached = _balance_cache.get(uid)

    if cached and (time.time() - cached[1]) < BALANCE_TTL:
        return cached[0]

    try:
        from firebase_admin import firestore

        snapshot = (
            firestore.client()
            .collection("users")
            .document(uid)
            .get()
        )

        if not snapshot.exists:
            return None

        seconds = (snapshot.to_dict() or {}).get("remainingSeconds")

        if seconds is None:
            return None

        seconds = int(seconds)
        _balance_cache[uid] = (seconds, time.time())

        return seconds

    except Exception as error:
        print("AUTH: balans o'qishda xato —", error)
        return None


def require_access(authorization):
    """
    Har bir qimmat endpoint boshida chaqiriladi.

    REQUIRE_AUTH o'chiq bo'lsa hech kimni rad etmaydi — faqat log yozadi,
    shunda yangi ilova qanchalik tarqalganini o'lchay olasiz.

    Qaytaradi: uid yoki None.
    """
    uid = verify_uid(authorization)

    if uid is None:
        print("AUTH: token yo'q yoki yaroqsiz")

        if REQUIRE_AUTH:
            raise HTTPException(
                status_code=401,
                detail="Ilovaga kirish talab qilinadi."
            )

        return None

    print("AUTH: token bor uid=%s" % uid)

    if not REQUIRE_BALANCE:
        return uid

    balance = get_balance_seconds(uid)

    if balance is None:
        # Yozuv topilmadi. Yangi foydalanuvchini bloklamaymiz — bepul vaqt
        # hali Firestore'ga yozilmagan bo'lishi mumkin.
        print("AUTH: balans yozuvi yo'q, o'tkazildi uid=%s" % uid)
        return uid

    if balance <= 0:
        raise HTTPException(
            status_code=402,
            detail="Vaqtingiz tugagan. Iltimos, vaqt sotib oling."
        )

    return uid


def invalidate_balance(uid):
    """Balans o'zgarganda keshni tozalash uchun."""
    _balance_cache.pop(uid, None)
