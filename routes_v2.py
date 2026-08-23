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

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query

from cache import get_cache, set_cache, TRANSLATION_TTL
from concurrency import cold_slot, single_flight, work_pool, ServerBusy
from redis_manager import redis_client
from translator import (
    translate_transcript,
    translate_range,
    translate_range_paired,
    translate_range_strict,
    slice_by_time,
    settings_fingerprint,
)

# /transcript bilan BIR XIL rejim tanlanishi uchun — ikkalasi ayni mantiqdan
# foydalanishi shart, aks holda foydalanuvchi qaysi endpoint chaqirilganiga
# qarab boshqa sifat oladi.
PAIRED_ON = os.getenv("TRANSLATE_PAIRED") in ("1", "true", "yes", "on")

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
    # Sozlamalar barmoq izi kalitda — istalgan sozlama o'zgarsa kesh
    # o'z-o'zidan bekor bo'ladi
    return "uz:win:%s:%s:%s:%d:%d" % (
        settings_fingerprint(), video_id, mode, int(from_time), int(WINDOW))


def _index_range_for_time(items, from_time, window):
    """
    Vaqt oynasini SEGMENT INDEKSLARI diapazoniga aylantiradi.

    Nega kerak: `/transcript` index bo'yicha ishlaydi va butun paired mantiq
    o'sha yerda. Bu yerda ham xuddi shu funksiyani chaqirish uchun vaqtni
    indeksga o'girib olamiz — shunda ikkala endpoint AYNI kodni ishlatadi
    va sifat farq qilmaydi.

    return: (offset, count) yoki (None, 0)
    """
    to_time = from_time + window

    first = None
    last = None

    for i, it in enumerate(items):

        start = float(it.get("start", 0) or 0)
        end = start + float(it.get("duration", 0) or 0)

        if end <= from_time:
            continue

        if start >= to_time:
            break

        if first is None:
            first = i

        last = i

    if first is None:
        return None, 0

    return first, (last - first + 1)


def _is_real_translation(cues):
    """
    Bu natija HAQIQATAN tarjima qilinganmi?

    Nega kerak: model rad etsa (kunlik token limiti, 429, tarmoq uzilishi)
    translator xato QAYTARMAYDI — `translated` maydoniga inglizcha matnning
    O'ZINI qo'yadi (translator.py:573). Ilgari o'sha natija keshga tushardi
    va 30 kun davomida HAMMA foydalanuvchi o'sha videoni tarjimasiz ko'rardi.
    Bitta yuk cho'qqisi katalogni shu tarzda zaharlab ketishi mumkin edi.

    Baholash faqat HAQIQIY GAPLAR bo'yicha: "[Music]" kabi qisqa teglar
    tarjimasiz qolishi normal, ular hisobga olinmaydi.
    """
    if not cues:
        return False

    real = [
        c for c in cues
        if len((c.get("text") or "").split()) >= 3
    ]

    if not real:
        # Baholaydigan gap yo'q (butun oyna musiqa/shovqin) — to'g'ri deymiz
        return True

    same = 0

    for c in real:
        text = (c.get("text") or "").strip()
        uz = (c.get("translated") or "").strip()

        if text and uz == text:
            same += 1

    # Yarmidan ko'pi tarjimasiz bo'lsa — bu tarjima emas, javob bermagan model
    return same * 2 <= len(real)


def _has_subtitles(items):
    if not items:
        return False
    if len(items) == 1 and items[0].get("text") == "NO_SUBTITLE_AVAILABLE":
        return False
    return True


def _translate_window(video_id, from_time, video_title="", mode="sentence",
                      slot_wait=None):
    """
    Bitta oynani tarjima qiladi. Sinxron — executor'da chaqiriladi.

    IKKI HIMOYA bilan o'ralgan (ilgari ikkalasi ham faqat /transcript da
    bor edi, holbuki ilovalar aynan SHU yo'ldan yuradi):

      single_flight — bir xil videoni bir vaqtda ochgan 30 kishidan
                      faqat BITTASI tarjima qiladi, qolganlari tayyor
                      natijani kutadi. Busiz reklama kuni bitta video
                      olomon soniga ko'paytirilib tarjima qilinardi.

      cold_slot     — bir vaqtda nechta og'ir ish ketishi cheklanadi.
                      Busiz 100 so'rov 100 ta ish boshlab, thread'lar
                      tugab, OpenAI 429 qaytarardi va HAMMA sekinlashardi.
    """
    key = _window_key(video_id, from_time, mode)

    cached = get_cache(key)

    if cached is not None:

        if _is_real_translation(cached):
            print("WINDOW FROM REDIS:", key)
            return cached

        # Eski ZAHARLANGAN yozuv: limitga urilgan paytda tushgan tarjimasiz
        # matn. O'chiramiz — aks holda 30 kun shu holicha berilaverardi.
        print("WINDOW CACHE ZAHARLANGAN, qayta quriladi:", key)

        try:
            if redis_client is not None:
                redis_client.delete(key)
        except Exception as error:
            print("CACHE DELETE ERROR:", error)

    def produce():

        with cold_slot(wait=slot_wait):

            fetch_transcript, _ = _deps()
            items = fetch_transcript(video_id)

            if not _has_subtitles(items):
                return None

            # Vaqtni indeksga o'giramiz va /transcript ISHLATADIGAN AYNI
            # funksiyani chaqiramiz — ikkala endpoint bir xil natija berishi
            # shart.
            offset, count = _index_range_for_time(items, from_time, WINDOW)

            if offset is None:
                return []

            if PAIRED_ON:
                return translate_range_paired(
                    items, offset, count, video_title=video_title
                )

            return translate_range_strict(
                items, offset, count, video_title=video_title
            )

    # DIQQAT: `index` ATAYLAB qayta raqamlanmaydi.
    #
    # U segmentning ABSOLYUT indeksi bo'lib qoladi. Ilova bir necha oynani
    # birlashtirganda takrorlarni aynan shu bo'yicha filtrlaydi — qayta
    # raqamlasak, har oyna 0 dan boshlanib, birlashtirish buzilardi.
    #
    # Keshlashni endi single_flight bajaradi va FAQAT haqiqiy tarjimani
    # yozadi (`cacheable`).
    return single_flight(
        result_key=key,
        ttl=TRANSLATION_TTL,
        produce=produce,
        cacheable=_is_real_translation,
    )


def _prefetch(video_id, from_time, video_title, mode):
    """Fon vazifasi: keyingi oynalarni oldindan tarjima qilib keshga qo'yadi."""
    for i in range(1, PREFETCH_AHEAD + 1):
        try:
            # slot_wait=0 — joy bo'sh bo'lmasa DARHOL voz kechadi.
            # Prefetch'ni hech kim kutmayapti; navbatda turib thread
            # ushlasa, haqiqiy so'rovlarga xalaqit beradi.
            _translate_window(
                video_id, from_time + WINDOW * i, video_title, mode,
                slot_wait=0,
            )
        except ServerBusy:
            print("PREFETCH: server band, oldindan tarjima o'tkazib yuborildi")
            break
        except Exception as error:
            print("PREFETCH ERROR:", error)


def _busy():
    """
    "Server band" javobi — 503.

    Nega `{"error": true}` EMAS: ilovalarda u "bu videoda subtitr yo'q"
    degan xabarga bog'langan. Server band bo'lganda o'sha xabarni
    ko'rsatish foydalanuvchini adashtiradi — video aybdor bo'lib chiqadi.

    503 esa ikkala ilovada ham tabiiy ravishda QAYTA URINISHGA olib
    keladi: Android istisno deb ushlaydi va 3 sekunddan keyin qayta
    so'raydi, iOS javobni o'qiy olmay o'sha oynani yuklanmagan deb
    qoldiradi. Ya'ni telefondagi ESKI versiyalar ham to'g'ri ishlaydi.
    """
    return HTTPException(
        status_code=503,
        detail="Server hozir band. Bir necha soniyadan keyin qayta urinib ko'ring.",
        headers={"Retry-After": "5"},
    )


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

    ENG MUHIMI: istalgan `t` ni berish mumkin. Foydalanuvchi videoni
    20-daqiqaga sursa `?t=1200` yuboriladi va subtitr DARHOL keladi.
    Eski `/transcript` da bu imkonsiz edi — u faqat sahifama-sahifa,
    boshidan oldinga yura olardi.

    `mode` parametri endi xatti-harakatni O'ZGARTIRMAYDI. Paired yoki
    strict tanlovi `/transcript` dagi kabi `TRANSLATE_PAIRED` env'idan
    olinadi, shunda ikkala endpoint bir xil natija beradi. Parametr
    faqat eski chaqiruvlar buzilmasligi uchun qoldirilgan.
    """
    if mode not in ("sentence", "segment"):
        mode = "sentence"

    from_time = _align(t)

    loop = asyncio.get_running_loop()

    try:
        # work_pool, `None` emas: standart executor min(32, cpu+4) thread
        # beradi va single-flight kutuvchilari ham thread egallaydi.
        cues = await loop.run_in_executor(
            work_pool,
            lambda: _translate_window(video_id, from_time, title, mode),
        )

    except ServerBusy:
        raise _busy()

    if cues is None:
        return {
            "error": True,
            "message": "Bu videoda subtitr mavjud emas.",
            "subtitles": [],
        }

    # Tarjima o'rniga inglizcha matn qaytmasin. Model rad etgan bo'lsa
    # (kunlik limit, 429) foydalanuvchi tarjimasiz matnni "tarjima" deb
    # ko'radi va daqiqasi ham yechiladi — bu eng yomon holat.
    if cues and not _is_real_translation(cues):
        print("WINDOW TARJIMASIZ QAYTDI:", video_id, from_time)
        raise _busy()

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

    video = await loop.run_in_executor(
        work_pool, lambda: get_video_url(video_id))

    title = video.get("title", "") or ""

    try:
        cues = await loop.run_in_executor(
            work_pool,
            lambda: _translate_window(video_id, 0.0, title, mode),
        )

    except ServerBusy:
        raise _busy()

    if cues is None:
        return {
            "error": True,
            "message": "Bu videoda subtitr mavjud emas.",
            "video_url": video.get("video_url", ""),
            "title": title,
            "thumbnail": video.get("thumbnail", ""),
            "subtitles": [],
        }

    if cues and not _is_real_translation(cues):
        print("WINDOW TARJIMASIZ QAYTDI:", video_id, 0.0)
        raise _busy()

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
