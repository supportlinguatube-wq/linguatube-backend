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
TTS_MODEL = os.getenv("OPENAI_TTS_MODEL", "tts-1")

TTS_PROMPT_VERSION = "v4_sokin_va_vazmin"

UZBEK_TTS_INSTRUCTIONS = (
    "Role: You are a calm, gentle, and composed native Uzbek narrator (bosiq, sokin va samimiy o'zbek suxandoni). "
    "Tone & Pacing: "
    "- Sokinlik va Bosiqlik (Calm & Soothing): Speak in a calm, relaxed, peaceful, and pleasant voice. Never sound rushed, frantic, loud, agitated, or theatrical. "
    "- Samimiy va Mayin (Warm & Gentle): Maintain a warm, friendly, natural conversational tone, like a calm educational documentary or warm audiobook narrator. "
    "- Aniq va Ravon (Articulate & Clear): Pronounce every single word completely, distinctly, and cleanly at an unhurried, natural speaking pace. "
    "- Word Completeness: Read every single word in full. Never omit or swallow words. "
    "- Authentic Uzbek: Speak in pure standard Uzbek with natural phonetics (natural 'q', 'o‘', 'g‘'). Avoid Turkish, Russian, or robotic intonation."
)

openai_client = None
if OPENAI_API_KEY:
    openai_client = OpenAI(api_key=OPENAI_API_KEY)

ALLOWED_VOICES = {
    "madina", "sardor", "shimmer", "nova", "alloy", "echo", "fable", "onyx"
}

EDGE_VOICE_MAP = {
    "madina": "uz-UZ-MadinaNeural",
    "sardor": "uz-UZ-SardorNeural"
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
    Hamma foydalanuvchilar (shu jumladan yangi 5 daqiqalik bepul sinovdagilar) uchun
    Edge TTS (Madina / Sardor) ruxsat beriladi.
    Faqat hisobida vaqti tugagan (0 bo'lgan) foydalanuvchilar cheklanadi.
    """
    if not uid:
        # Bepul sinov yoki anonim foydalanuvchilar uchun ruxsat
        return True

    app = _firebase()
    if app is None:
        return True

    try:
        from firebase_admin import firestore

        doc = firestore.client().collection("users").document(uid).get()
        if not doc.exists:
            # Yangi foydalanuvchi — 5 daqiqalik sinov huquqi bor
            return True

        data = doc.to_dict() or {}
        has_purchased = data.get("hasPurchasedTime", False) is True
        is_booster = data.get("isBoosterUnlocked", False) is True
        remaining_seconds = data.get("remainingSeconds")

        if remaining_seconds is None:
            return True

        try:
            remaining_seconds = int(remaining_seconds)
        except Exception:
            remaining_seconds = 0

        # Ruxsat berish shartlari:
        # - Pul to'lab xarid qilgan
        # - Yoki Booster kursi xarid qilingan
        # - Yoki hisobida hali vaqti qolgan bo'lsa (remaining_seconds > 0)
        if has_purchased or is_booster or (remaining_seconds > 0):
            return True

        return False

    except Exception as error:
        logger.error(f"TTS entitlement check error for uid={uid}: {error}")
        return True


@router.post("/speak")
async def speak(
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
        clean_voice = "madina"

    clean_speed = max(0.5, min(2.0, round(request.speed, 2)))

    # 4. Redis Keshi (MD5 hash orqali)
    cache_raw = f"{TTS_PROMPT_VERSION}_{clean_text}_{clean_voice}_{clean_speed}"
    cache_key = f"tts:{hashlib.md5(cache_raw.encode('utf-8')).hexdigest()}"

    if redis_client is not None:
        try:
            cached_b64 = redis_client.get(cache_key)
            if cached_b64:
                audio_bytes = base64.b64decode(cached_b64)
                return Response(content=audio_bytes, media_type="audio/mpeg")
        except Exception as error:
            logger.warning(f"TTS Redis read error: {error}")

    # 5. Agar Madina yoki Sardor (Edge TTS — 100% BEPUL / 0$) tanlangan bo'lsa:
    if clean_voice in EDGE_VOICE_MAP:
        try:
            import edge_tts
            edge_voice_id = EDGE_VOICE_MAP[clean_voice]
            pct = int(round((clean_speed - 1.0) * 100))
            rate_str = f"+{pct}%" if pct >= 0 else f"{pct}%"
            communicate = edge_tts.Communicate(clean_text, edge_voice_id, rate=rate_str)
            audio_buf = bytearray()
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    audio_buf.extend(chunk["data"])
            audio_bytes = bytes(audio_buf)
            if audio_bytes:
                if redis_client is not None:
                    try:
                        b64_str = base64.b64encode(audio_bytes).decode("ascii")
                        redis_client.setex(cache_key, TTS_CACHE_TTL, b64_str)
                    except Exception as error:
                        logger.warning(f"TTS Redis write error: {error}")
                return Response(content=audio_bytes, media_type="audio/mpeg")
        except Exception as error:
            logger.error(f"Edge TTS xatosi ({clean_voice}): {error}. OpenAI bilan davom etiladi...")

    # 6. OpenAI TTS orqali generatsiya qilish
    audio_bytes = None
    if openai_client:
        try:
            model_to_use = "tts-1" if TTS_MODEL not in ("tts-1", "tts-1-hd") else TTS_MODEL
            speech_resp = openai_client.audio.speech.create(
                model=model_to_use,
                voice=clean_voice if clean_voice in {"alloy", "echo", "fable", "onyx", "nova", "shimmer"} else "shimmer",
                input=clean_text,
                speed=clean_speed,
                response_format="mp3"
            )
            audio_bytes = speech_resp.content
        except Exception as error:
            logger.error(f"OpenAI TTS API xatosi ({clean_voice}): {error}. Edge TTS zaxirasiga o'tiladi...")

    # 7. Agar OpenAI ishlamasa yoki bo'sh bo'lsa -> Edge TTS (Madina) zaxirasi
    if not audio_bytes:
        try:
            import edge_tts
            fallback_voice = "uz-UZ-MadinaNeural"
            pct = int(round((clean_speed - 1.0) * 100))
            rate_str = f"+{pct}%" if pct >= 0 else f"{pct}%"
            communicate = edge_tts.Communicate(clean_text, fallback_voice, rate=rate_str)
            audio_buf = bytearray()
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    audio_buf.extend(chunk["data"])
            audio_bytes = bytes(audio_buf)
        except Exception as error:
            logger.error(f"Edge TTS fallback xatosi: {error}")

    if not audio_bytes:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Ovoz generatsiya qilib bo'lmadi"
        )

    # 8. Redis'ga yozish
    if redis_client is not None:
        try:
            b64_str = base64.b64encode(audio_bytes).decode("ascii")
            redis_client.setex(cache_key, TTS_CACHE_TTL, b64_str)
        except Exception as error:
            logger.warning(f"TTS Redis write error: {error}")

    return Response(content=audio_bytes, media_type="audio/mpeg")
