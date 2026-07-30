"""
routes_v2.py — yangi subtitr endpoint'lari
==========================================
main.py dagi eski endpoint'lar TEGILMAGAN. Bu router ular bilan yonma-yon
ishlaydi. Frontend tayyor bo'lganda /v2/... ga o'tadi; muammo chiqsa
main.py dagi bitta `include_router` qatorini izohga olib qaytarasiz.

MUAMMO: eski oqimda `limit=40` bor edi. Auto-caption segmenti ~3 sekund,
        40 x 3s = 120s. Ya'ni "2 daqiqadan keyin uzulish" — tarjima sifati
        emas, birinchi chunk tugagan joy. Frontend keyingi chunkni faqat
        kerak bo'lganda so'raydi, backend esa 40 ta alohida OpenAI so'rovini
        5 worker bilan bajaradi (8 raund ~ 10-20 s). O'sha 10-20 sekund
        ekranda bo'shliq.

YECHIM: index pagination o'rniga VAQT OYNASI + keyingi oynalarni FONDA
        oldindan tarjima qilish. Frontend oyna tugashiga 30 s qolganda
        so'raydi, kesh allaqachon iliq -> javob oniy.
"""

import asyncio
import os

from fastapi import APIRouter, BackgroundTasks, Query

from cache import get_cache, set_cache, TRANSLATION_TTL
from translator import (
    translate_transcript,
    translate_range,
    slice_by_time,
    PROMPT_VERSION,
)

router = APIRouter(prefix="/v2", tags=["v2"])

WINDOW = float(os.getenv("SUBTITLE_WINDOW", "90"))   # bir oyna necha sekund
PREFETCH_AHEAD = int(os.getenv("SUBTITLE_PREFETCH", "2"))
CONTEXT_PAD = 10.0        # oynadan oldin kontekst uchun qo'shiladigan sekund


def _deps():
    """
    main.py dan kech (lazy) import — aylanma import bo'lmasligi uchun.
    main.py bu modulni import qiladi, shuning uchun modul yuklanish
    vaqtida main dan import qilib bo'lmaydi.
    """
    from main import fetch_transcript, get_video_url
    return fetch_transcript, get_video_url


def _window_key(video_id, from_time, mode):
    # PROMPT_VERSION kalitda — prompt o'zgarsa kesh o'zi bekor bo'ladi
    return "uz:win:%s:%s:%s:%d:%d" % (
        PROMPT_VERSION, video_id, mode, int(from_time), int(WINDOW))


def _has_subtitles(items):
    if not items:
        return False
    if len(items) == 1 and items[0].get("text") == "NO_SUBTITLE_AVAILABLE":
        return False
    return True


def _translate_window(video_id, from_time, video_title="", mode="sentence"):
    """Bitta oynani tarjima qiladi (kesh bilan). Sinxron — executor'da chaqiriladi."""
    key = _window_key(video_id, from_time, mode)

    cached = get_cache(key)
    if cached is not None:
        print("WINDOW FROM REDIS:", key)
        return cached

    fetch_transcript, _ = _deps()
    items = fetch_transcript(video_id)

    if not _has_subtitles(items):
        return None

    # Kontekst uchun oynadan 10 s oldin boshlaymiz, keyin kesib tashlaymiz
    raw = slice_by_time(items, max(0.0, from_time - CONTEXT_PAD),
                        WINDOW + CONTEXT_PAD)
    if not raw:
        return []

    cues = translate_transcript(raw, video_title=video_title, mode=mode)
    cues = [c for c in cues if c["start"] + c["duration"] > from_time]

    for i, c in enumerate(cues):
        c["index"] = i

    set_cache(key, cues, TRANSLATION_TTL)
    return cues


def _prefetch(video_id, from_time, video_title, mode):
    """Fon vazifasi: keyingi oynalarni oldindan tarjima qilib keshga qo'yadi."""
    for i in range(1, PREFETCH_AHEAD + 1):
        try:
            _translate_window(video_id, from_time + WINDOW * i, video_title, mode)
        except Exception as error:
            print("PREFETCH ERROR:", error)


def _align(t):
    """Oyna boshiga tekislash — shu sabab kesh kaliti barqaror bo'ladi."""
    if t <= 0:
        return 0.0
    return float(int(t / WINDOW) * WINDOW)


@router.get("/subtitles/{video_id}")
async def v2_subtitles(
    video_id: str,
    background: BackgroundTasks,
    t: float = Query(default=0.0, description="pleyerning hozirgi vaqti (sekund)"),
    mode: str = Query(default="sentence"),
    title: str = Query(default=""),
):
    """
    Frontend: /v2/subtitles/VIDEO_ID?t=0  -> keyin ?t=90 -> ?t=180 ...
    `next_t` javobda qaytadi, frontend shuni ishlatadi.
    """
    if mode not in ("sentence", "segment"):
        mode = "sentence"

    from_time = _align(t)

    loop = asyncio.get_running_loop()
    cues = await loop.run_in_executor(
        None,
        lambda: _translate_window(video_id, from_time, title, mode),
    )

    if cues is None:
        return {
            "error": True,
            "message": "Bu videoda subtitr mavjud emas.",
            "subtitles": [],
        }

    background.add_task(_prefetch, video_id, from_time, title, mode)

    return {
        "window": {"from": from_time, "to": from_time + WINDOW},
        "next_t": from_time + WINDOW,
        "count": len(cues),
        "subtitles": cues,
    }


@router.get("/process/{video_id}")
async def v2_process(
    video_id: str,
    background: BackgroundTasks,
    mode: str = Query(default="sentence"),
):
    """Video meta + birinchi oyna, bittada. Eski /process ning o'rnini bosadi."""
    if mode not in ("sentence", "segment"):
        mode = "sentence"

    _, get_video_url = _deps()
    loop = asyncio.get_running_loop()

    video = await loop.run_in_executor(None, lambda: get_video_url(video_id))
    title = video.get("title", "") or ""

    cues = await loop.run_in_executor(
        None,
        lambda: _translate_window(video_id, 0.0, title, mode),
    )

    if cues is None:
        return {
            "error": True,
            "message": "Bu videoda subtitr mavjud emas.",
            "video_url": video.get("video_url", ""),
            "title": title,
            "thumbnail": video.get("thumbnail", ""),
            "subtitles": [],
        }

    background.add_task(_prefetch, video_id, 0.0, title, mode)

    return {
        "video_url": video.get("video_url", ""),
        "title": title,
        "thumbnail": video.get("thumbnail", ""),
        "window": {"from": 0.0, "to": WINDOW},
        "next_t": WINDOW,
        "count": len(cues),
        "subtitles": cues,
    }


@router.get("/transcript/{video_id}")
def v2_transcript(
    video_id: str,
    limit: int = Query(default=40),
    offset: int = Query(default=0),
    mode: str = Query(default="segment"),
):
    """
    Eski /transcript bilan BIR XIL shakl (oddiy massiv, index/text/translated/
    start/duration) — lekin yangi pipeline ustida. Frontend'ni o'zgartirmasdan
    sifatni tekshirib ko'rish uchun: URL dan `/transcript` -> `/v2/transcript`.

    Chunk chegarasida gap kesilmasligi uchun oldi/orqadan 6 segment kontekst
    olinadi (translate_range ichida).
    """
    if mode not in ("sentence", "segment"):
        mode = "segment"

    fetch_transcript, _ = _deps()
    items = fetch_transcript(video_id)

    if not _has_subtitles(items):
        return {"error": True, "message": "Bu videoda subtitr mavjud emas."}

    key = "uz:rng:%s:%s:%s:%d:%d" % (PROMPT_VERSION, video_id, mode, offset, limit)
    cached = get_cache(key)
    if cached is not None:
        return cached

    cues = translate_range(items, offset, limit, mode=mode)
    set_cache(key, cues, TRANSLATION_TTL)
    return cues
