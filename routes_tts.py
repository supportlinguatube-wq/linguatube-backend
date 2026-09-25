"""
routes_tts.py — O'zbekcha AI Ovoz (OpenAI TTS) endpointi
========================================================

- POST /speak
- Faqat vaqt xarid qilgan (hasPurchasedTime) yoki 10 soatdan ko'p vaqti bor (promo code / admin) foydalanuvchilar kira oladi.
- Bepul sinovdagi (5 daqiqa) foydalanuvchilar HTTP 403 bilan rad etiladi.
- Redis keshlash: bir xil matn + ovoz + tezlik uchun takroriy OpenAI so'rovi ketmaydi.
"""

import os
import hashlib
import base64
import logging
from typing import Optional
from fastapi import APIRouter, Header, HTTPException, Response, status
from pydantic import BaseModel, Field
from openai import OpenAI

from auth import verify_uid, _firebase
from redis_manager import redis_client

logger = logging.getLogger("linguatube.tts")

router = APIRouter(tags=["AI Voice / TTS"])

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
TTS_MODEL = os.getenv("OPENAI_TTS_MODEL", "gpt-4o-mini-tts")

UZBEK_TTS_INSTRUCTIONS = (
    "Speak in fluent, natural, native Uzbek language with an authentic Uzbek accent. "
    "Pronounce the Uzbek letter 'q' (Q) distinctly as a deep uvular stop [q], never as Turkish 'k'. "
    "Pronounce 'o‘' (o') and 'g‘' (g') accurately according to authentic Uzbek phonetics. "
    "Do not speak with a Turkish or foreign accent. Maintain clear, natural Uzbek intonation."
)

openai_client = None
if OPENAI_API_KEY:
    openai_client = OpenAI(api_key=OPENAI_API_KEY)

ALLOWED_VOICES = {
    "shimmer", "nova", "alloy", "echo", "fable", "onyx"
}

# 10 soat = 36000 sekund (admin/developer promo code orqali vaqt qo'shganda)
ADMIN_PROMO_THRESHOLD_SECONDS = 10 * 3600
TTS_CACHE_TTL = 60 * 60 * 24 * 14  # 14 kun

class SpeakRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=1500)
    voice: str = Field(default="shimmer")
    speed: float = Field(default=1.0, ge=0.5, le=2.0)

def check_tts_entitlement(uid: Optional[str]) -> bool:
    """
    Foydalanuvchining AI Ovozdan foydalanish huquqini tekshiradi:
    1. Vaqt xarid qilgan (hasPurchasedTime == True)
    2. Yoki Booster kursi ochilgan (isBoosterUnlocked == True)
    3. Yoki hisobida 10 soatdan ko'p vaqti bor (remainingSeconds >= 36000)
       (Admin/developer promo code orqali o'ziga vaqt qo'shganda).

    5 daqiqalik bepul foydalanuvchilar uchun False qaytaradi.
    """
    if not uid:
        return False

    app = _firebase()
    if app is None:
        # Dev rejim: agar FIREBASE_SERVICE_ACCOUNT_JSON bo'lmasa,
        # faqat maxsus DEV_ALLOW_TTS=1 bo'lsagina ruxsat beriladi (default xavfsiz: False)
        return os.getenv("DEV_ALLOW_TTS") in ("1", "true", "yes", "on")

    try:
        from firebase_admin import firestore

        doc = firestore.client().collection("users").document(uid).get()
        if not doc.exists:
            return False

        data = doc.to_dict() or {}
        has_purchased = data.get("hasPurchasedTime", False) is True
        is_booster = data.get("isBoosterUnlocked", False) is True
        remaining_seconds = data.get("remainingSeconds") or 0

        try:
            remaining_seconds = int(remaining_seconds)
        except Exception:
            remaining_seconds = 0

        # Ruxsat berish shartlari:
        # - Pul to'lab xarid qilgan
        # - Yoki Booster kursi xarid qilingan
        # - Yoki 10 soatdan ko'p vaqt mavjud (admin promo code)
        if has_purchased or is_booster or (remaining_seconds >= ADMIN_PROMO_THRESHOLD_SECONDS):
            return True

        return False

    except Exception as error:
        logger.error(f"TTS entitlement check error for uid={uid}: {error}")
        return False


@router.post("/speak")
def speak(
    request: SpeakRequest,
    authorization: Optional[str] = Header(default=None),
    x_user_id: Optional[str] = Header(default=None)
):
    """
    Subtitr matnidan O'zbekcha AI audio (MP3) generatsiya qiladi.
    """
    # 1. Foydalanuvchini aniqlash
    uid = None
    if authorization:
        uid = verify_uid(authorization)

    if not uid and x_user_id:
        uid = x_user_id.strip()

    # 2. Xarid yoki 10+ soat vaqt huquqini tekshirish
    if not check_tts_entitlement(uid):
        logger.warning(f"TTS 403 rad etildi: uid={uid}")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="AI voice is available after purchasing time."
        )

    # 3. Parametrlarni tozalash va tekshirish
    clean_text = request.text.strip()
    if not clean_text:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Text cannot be empty."
        )

    clean_voice = request.voice.lower().strip()
    if clean_voice not in ALLOWED_VOICES:
        clean_voice = "shimmer"

    clean_speed = max(0.5, min(2.0, round(request.speed, 2)))

    # 4. Redis Keshi (MD5 hash orqali)
    cache_raw = f"{clean_text}_{clean_voice}_{clean_speed}"
    cache_key = f"tts:{hashlib.md5(cache_raw.encode('utf-8')).hexdigest()}"

    if redis_client is not None:
        try:
            cached_b64 = redis_client.get(cache_key)
            if cached_b64:
                audio_bytes = base64.b64decode(cached_b64)
                return Response(content=audio_bytes, media_type="audio/mpeg")
        except Exception as error:
            logger.warning(f"TTS Redis read error: {error}")

    # 5. OpenAI TTS orqali generatsiya qilish
    if not openai_client:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="OPENAI_API_KEY is not configured on server."
        )

    try:
        create_kwargs = {
            "model": TTS_MODEL,
            "voice": clean_voice,
            "input": clean_text,
            "speed": clean_speed,
            "response_format": "mp3"
        }
        if "gpt-4o" in TTS_MODEL or "mini" in TTS_MODEL:
            create_kwargs["instructions"] = UZBEK_TTS_INSTRUCTIONS

        speech_resp = openai_client.audio.speech.create(**create_kwargs)

        audio_bytes = speech_resp.content

        # 6. Redis'ga yozish
        if redis_client is not None:
            try:
                b64_str = base64.b64encode(audio_bytes).decode("ascii")
                redis_client.setex(cache_key, TTS_CACHE_TTL, b64_str)
            except Exception as error:
                logger.warning(f"TTS Redis write error: {error}")

        return Response(content=audio_bytes, media_type="audio/mpeg")

    except HTTPException:
        raise
    except Exception as error:
        logger.error(f"OpenAI TTS API xatosi: {error}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"TTS generation error: {str(error)}"
        )
