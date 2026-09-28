"""
routes_tts.py — O'zbekcha AI Ovoz (OpenAI TTS) endpointi
========================================================

- POST /speak
- Faqat vaqt xarid qilgan (hasPurchasedTime) yoki 10 soatdan ko'p vaqti bor (promo code / admin) foydalanuvchilar kira oladi.
- Bepul sinovdagi (5 daqiqa) foydalanuvchilar HTTP 403 bilan rad etiladi.
- Redis keshlash: bir xil matn + ovoz + tezlik uchun takroriy OpenAI so'rovi ketmaydi.
"""

import os
import re
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

TTS_PROMPT_VERSION = "v5_clean_phonetics"

# --- O'zbek tili uchun fonetik va raqamlar normalizatori ---
ONES_UZ = {
    0: "nol", 1: "bir", 2: "ikki", 3: "uch", 4: "toʻrt", 5: "besh",
    6: "olti", 7: "yetti", 8: "sakkiz", 9: "toʻqqiz"
}
TENS_UZ = {
    10: "oʻn", 20: "yigirma", 30: "oʻttiz", 40: "qirq", 50: "ellik",
    60: "oltmish", 70: "yetmish", 80: "sakson", 90: "toʻqson"
}

def int_to_uzbek(n: int) -> str:
    if n == 0:
        return "nol"
    if n < 0:
        return "minus " + int_to_uzbek(-n)
    parts = []
    if n >= 1_000_000_000:
        b = n // 1_000_000_000
        n %= 1_000_000_000
        parts.append(int_to_uzbek(b) + " milliard")
    if n >= 1_000_000:
        m = n // 1_000_000
        n %= 1_000_000
        parts.append(int_to_uzbek(m) + " million")
    if n >= 1000:
        t = n // 1000
        n %= 1000
        parts.append((int_to_uzbek(t) if t > 1 else "bir") + " ming")
    if n >= 100:
        h = n // 100
        n %= 100
        parts.append((ONES_UZ[h] if h > 1 else "") + " yuz")
    if n >= 10:
        ten = (n // 10) * 10
        n %= 10
        parts.append(TENS_UZ[ten])
    if n > 0:
        parts.append(ONES_UZ[n])
    return " ".join([p for p in parts if p.strip()])

def ordinal_suffix(word: str) -> str:
    w = word.strip()
    if w.endswith("i") or w.endswith("a"):
        return w + "nchi"
    return w + "inchi"

def clean_uzbek_text_for_tts(text: str) -> str:
    """
    TTS modellari (Edge TTS va OpenAI) o'zbekcha matnni xatosiz va tabiiy o'qishi uchun:
    1. Raqamlar (3000 -> uch ming) o'zbek so'zlariga aylantiriladi ('3 oh oh oh' muammosi hal bo'ladi).
    2. Oʻ va Gʻ harflaridagi har xil belgilar (’, ‘, ', `, ʼ) rasmiy o'zbek \\u02bb harfiga keltiriladi ('o' deb o'qish yo'qoladi).
    3. Gap boshidagi kesilishlarning oldini olish uchun tabiiy mikropauza qo'yiladi.
    """
    if not text:
        return ""
    t = text

    # 1. Standartlashtirish: Oʻ va Gʻ harflaridagi har xil apostroflarni (’, ‘, ', `, ʼ)
    # rasmiy oʻzbek lotin oʻzgartiruvchi belgisi \u02bb ga aylantiramiz
    t = re.sub(r"[oO][\u2019\u2018\x27\x60\u02bc\u02bb]", lambda m: "Oʻ" if m.group(0)[0] == "O" else "oʻ", t)
    t = re.sub(r"[gG][\u2019\u2018\x27\x60\u02bc\u02bb]", lambda m: "Gʻ" if m.group(0)[0] == "G" else "gʻ", t)

    # Standart so'z ichidagi tutuq belgisi (masalan: ma'lumot, ta'lim, e'tibor)
    t = re.sub(r"([a-zA-Z])[\u2019\u2018\x60\u02bc]([a-zA-Z])", r"\1'\2", t)

    # 2. Foizlar (%50 yoki 50%)
    t = re.sub(r"%\s*(\d+)", lambda m: int_to_uzbek(int(m.group(1))) + " foiz", t)
    t = re.sub(r"(\d+)\s*%", lambda m: int_to_uzbek(int(m.group(1))) + " foiz", t)

    # 3. Valyutalar ($100 yoki 100$)
    t = re.sub(r"\$\s*(\d+)", lambda m: int_to_uzbek(int(m.group(1))) + " dollar", t)
    t = re.sub(r"(\d+)\s*\$", lambda m: int_to_uzbek(int(m.group(1))) + " dollar", t)

    # 4. Oʻnlik kasrlar: 3.5 yoki 3,5
    t = re.sub(r"(\d+)[.,](\d+)", lambda m: int_to_uzbek(int(m.group(1))) + " butun " + int_to_uzbek(int(m.group(2))), t)

    # 5. Tartib sonlar: 1-oʻrin, 2-chi, 3000-
    t = re.sub(r"(\d+)-(?:chi|inchi)?\b", lambda m: ordinal_suffix(int_to_uzbek(int(m.group(1)))) + " ", t)

    # 6. Oddiy butun sonlar: 3000 -> uch ming
    t = re.sub(r"\b\d+\b", lambda m: int_to_uzbek(int(m.group(0))), t)

    # 7. Ortiqcha belgilarni tozalash (emoji, maxsus belgilar)
    t = re.sub(r"[\r\n\t]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()

    return t

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
    clean_text = clean_uzbek_text_for_tts(request.text.strip())
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
