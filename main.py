from fastapi import FastAPI, Query, Header
from youtube_transcript_api import YouTubeTranscriptApi
from openai import OpenAI
from dotenv import load_dotenv
from concurrent.futures import ThreadPoolExecutor
from auth import (
    require_access,
    check_access,
    update_notice_subtitles,
    UPDATE_NOTICE_UZ,
)
from cache import (
    get_cache,
    set_cache,
    TRANSCRIPT_TTL,
    TRANSLATION_TTL,
    VIDEO_URL_TTL,
    WORD_TTL
)


import random
import os
import json
import re
import ftfy
import subprocess
import tempfile
import asyncio
import httpx
import hashlib
from urllib.parse import urlparse, urlunparse
from importlib.metadata import version

print("youtube-transcript-api version:", version("youtube-transcript-api"))


load_dotenv()

app = FastAPI()

# =========================
# CORS
# Oldin butunlay yo'q edi. Brauzerdan (web yoki extension) chaqirilsa
# so'rov CORS xatosi bilan bloklanadi. Mobil ilova uchun ta'siri yo'q.
# ALLOWED_ORIGINS env: "https://linguatube.uz,https://www.linguatube.uz"
# =========================
from fastapi.middleware.cors import CORSMiddleware

_origins = [
    o.strip()
    for o in os.getenv("ALLOWED_ORIGINS", "*").split(",")
    if o.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials=("*" not in _origins),
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/version")
def get_version():
    return {
        "youtube_transcript_api": version("youtube-transcript-api")
    }

client = OpenAI(
    api_key=os.getenv("OPENAI_API_KEY")
)

MODEL = "gpt-4.1-mini"

VIDEO_PROXY_CACHE = {}

PROXY_URL = os.getenv("YTDLP_PROXY")

if PROXY_URL:
    print("PROXY ENABLED")
else:
    print("NO PROXY")
# =========================

def get_proxy_url(video_id: str = None):

    if not PROXY_URL:
        return None

    parsed = urlparse(PROXY_URL)

    # Shu video uchun oldingi ishlagan portni ishlatamiz
    if video_id and video_id in VIDEO_PROXY_CACHE:
        port = VIDEO_PROXY_CACHE[video_id]
    else:
        port = random.randint(10000, 20000)
        VIDEO_PROXY_CACHE[video_id] = port

    netloc = f"{parsed.username}:{parsed.password}@{parsed.hostname}:{port}"

    proxy_url = urlunparse((
        parsed.scheme,
        netloc,
        parsed.path,
        parsed.params,
        parsed.query,
        parsed.fragment,
    ))

    print(f"VIDEO {video_id} -> PORT {port}")

    return proxy_url

def rotate_proxy(video_id: str):

    # BUG EDI: PROXY_URL yo'q bo'lsa urlparse(None) AttributeError beradi.
    # rotate_proxy generator bo'lgani uchun xato faqat birinchi next() da
    # otiladi -> proxy sozlanmagan holatda birinchi fetch muvaffaqiyatsiz
    # bo'lsa, yt-dlp fallback'ga o'tmasdan butun so'rov qulaydi.
    if not PROXY_URL:
        return

    parsed = urlparse(PROXY_URL)

    for _ in range(10):

        port = random.randint(10000, 20000)

        VIDEO_PROXY_CACHE[video_id] = port

        netloc = f"{parsed.username}:{parsed.password}@{parsed.hostname}:{port}"

        proxy_url = urlunparse((
            parsed.scheme,
            netloc,
            parsed.path,
            parsed.params,
            parsed.query,
            parsed.fragment,
        ))

        print(f"TRY NEW PORT {port}")

        yield proxy_url
# CACHE
# =========================

TRANSCRIPT_CACHE = {}
TRANSLATION_CACHE = {}

# =========================
# LANGUAGES
# =========================

SUPPORTED = [
    "en",
    "ru",
    "ar",
    "zh",
    "ko",
    "ja"
]

# =========================
# ROOT
# =========================

@app.get("/")
def root():

    return {
        "message": "LinguaTube backend running"
    }

# =========================
# FIX TEXT
# =========================

def clean_text(text: str) -> str:

    if not isinstance(text, str):
        return ""

    return ftfy.fix_text(text).strip()



@app.get("/process/{video_id}")
async def process_video(
    video_id: str,
    limit: int = Query(default=40),
    offset: int = Query(default=0),
    authorization: str = Header(default=None)
):

    loop = asyncio.get_running_loop()

    transcript_future = loop.run_in_executor(
        None,
        lambda: get_transcript(
            video_id,
            limit,
            offset,
            "",
            authorization
        )
    )

    video_future = loop.run_in_executor(
        None,
        lambda: get_video_url(video_id)
    )

    subtitles, video = await asyncio.gather(
        transcript_future,
        video_future
    )

    return {
        "video_url": video["video_url"],
        "title": video["title"],
        "thumbnail": video["thumbnail"],
        "subtitles": subtitles
    }


LANGUAGE_CACHE = {}


def detect_video_language(video_id: str, proxy_url: str = None):

    # BUG EDI: bu funksiya fetch_with_youtube_transcript_api ichida chaqiriladi,
    # u esa proxy-rotation tsiklida 11 martagacha qayta chaqirilishi mumkin.
    # Har chaqiruvda TO'LIQ yt_dlp.extract_info ketadi -> katta latency.
    # Endi keshlanadi.
    if video_id in LANGUAGE_CACHE:
        return LANGUAGE_CACHE[video_id]

    cache_key = f"lang:{video_id}"
    cached = get_cache(cache_key)
    if cached is not None:
        LANGUAGE_CACHE[video_id] = cached
        return cached

    try:
        import yt_dlp

        url = f"https://www.youtube.com/watch?v={video_id}"

        ydl_opts = {
            "quiet": True,
            "skip_download": True,
        }

        if proxy_url:
            ydl_opts["proxy"] = proxy_url

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        language = (
            info.get("language")
            or info.get("default_language")
            or ""
        ).lower()

        print("VIDEO LANGUAGE:", language)

        LANGUAGE_CACHE[video_id] = language
        set_cache(cache_key, language, TRANSCRIPT_TTL)

        return language

    except Exception as e:

        print("VIDEO LANGUAGE ERROR:", e)

        return ""
# =========================
# FETCH TRANSCRIPT
# =========================

def fetch_transcript(video_id: str):

    # Kesh tekshiruvi PROXY tanlashdan OLDIN bo'lishi kerak — aks holda
    # har bir kesh-hitda bekorga tasodifiy port band qilinadi va log to'ladi.
    cache_key = f"transcript:{video_id}"

    cached = get_cache(cache_key)

    if cached is not None:
        print("TRANSCRIPT FROM REDIS")
        return cached

    if video_id in TRANSCRIPT_CACHE:
        print("TRANSCRIPT FROM MEMORY")
        return TRANSCRIPT_CACHE[video_id]

    proxy_url = get_proxy_url(video_id)

    # 1) FIRST: youtube-transcript-api
    items, permanent = fetch_with_youtube_transcript_api(video_id, proxy_url)

    # Xato YAKUNIY bo'lsa (subtitr o'chirilgan, video yo'q) proxy almashtirish
    # foydasiz — darhol to'xtaymiz. Faqat IP bloki kabi vaqtinchalik
    # nosozliklarda qayta uriniladi.
    if not items and not permanent:

        for proxy_url in rotate_proxy(video_id):

            items, permanent = fetch_with_youtube_transcript_api(
                video_id, proxy_url
            )

            if items or permanent:

                break


    if items:
        TRANSCRIPT_CACHE[video_id] = items

        set_cache(
           f"transcript:{video_id}",
           items,
           TRANSCRIPT_TTL
)

        return items

    # 2) SECOND: yt-dlp fallback
    #
    # Yakuniy xatoda ham BIR MARTA uriniladi — youtube-transcript-api
    # ba'zan topolmagan subtitrni yt-dlp topadi. Lekin proxy aylantirish
    # qilinmaydi, chunki YouTube "subtitr o'chirilgan" deb aytgan.
    items = fetch_with_ytdlp_subtitles(video_id, proxy_url)

    if not items and not permanent:
        for proxy_url in rotate_proxy(video_id):

            items = fetch_with_ytdlp_subtitles(video_id, proxy_url)

            if items:
                break

    if items:
        TRANSCRIPT_CACHE[video_id] = items

        set_cache(
            f"transcript:{video_id}",
            items,
            TRANSCRIPT_TTL
)

        return items

    

    # # 3) THIRD: audio -> OpenAI transcription
    # items = fetch_with_whisper(video_id)

    # if items:
    #     TRANSCRIPT_CACHE[video_id] = items
    #     return items

    return [{
    "text": "NO_SUBTITLE_AVAILABLE",
    "start": 0,
    "duration": 0
    }]
# =========================

def fetch_with_ytdlp_subtitles(video_id: str, proxy_url: str = None):

    try:
        import yt_dlp

        url = f"https://www.youtube.com/watch?v={video_id}"

        temp_dir = tempfile.mkdtemp()
        
        available_langs = []

        try:
            if proxy_url:
                http_client = httpx.Client(
                    proxy=proxy_url,
                    timeout=30.0,
                )
                api = YouTubeTranscriptApi(http_client=http_client)
            else:
                api = YouTubeTranscriptApi()

            transcript_list = api.list(video_id)

            available_langs = [
                t.language_code
                for t in transcript_list
            ]

        except Exception:
            pass

        preferred_order = [
            "en",
            "ru",
            "ar",
            "zh",
            "ko",
            "ja"
        ]

        lang = next(
            (
                l for l in preferred_order
                if any(
                    code.startswith(l)
                    for code in available_langs
                )
            ),
            None
        )

        subtitle_langs = [lang] if lang else ["en"]

        print("AVAILABLE LANGS:", available_langs)
        print("SELECTED LANG:", subtitle_langs)

        ydl_opts = {
            "skip_download": True,
            "writesubtitles": True,
            "writeautomaticsub": True,
            "subtitleslangs": subtitle_langs,
            "subtitlesformat": "json3",
            "outtmpl": os.path.join(
                temp_dir,
                "%(id)s.%(ext)s"
            ),
            "quiet": True,
            "ignoreerrors": True
            
        }
        if proxy_url:
            ydl_opts["proxy"] = proxy_url



        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.extract_info(url, download=True)

        json_files = [
            file for file in os.listdir(temp_dir)
            if file.endswith(".json3")
        ]

        if not json_files:
            print("YT-DLP SUBTITLE: NOT FOUND")
            return []

        subtitle_path = os.path.join(
            temp_dir,
            json_files[0]
        )

        with open(
            subtitle_path,
            "r",
            encoding="utf-8"
        ) as file:
            data = json.load(file)

        items = []

        for event in data.get("events", []):

            if "segs" not in event:
                continue

            text = "".join(
                seg.get("utf8", "")
                for seg in event.get("segs", [])
            )

            text = re.sub(
                r"\s+",
                " ",
                text
            ).strip()

            if not text:
                continue

            start = event.get(
                "tStartMs",
                0
            ) / 1000

            duration = event.get(
                "dDurationMs",
                0
            ) / 1000

            items.append({
                "text": clean_text(text),
                "start": start,
                "duration": duration
            })

        print("YT-DLP SUBTITLE OK:", len(items))

        return items

    except Exception as error:

        print("YT-DLP SUBTITLE ERROR:", error)

        return []
    

# def fetch_with_youtube_transcript_api(video_id: str):

#     try:
#         api = YouTubeTranscriptApi()

#         transcript_list = api.list(video_id)
def is_permanent_transcript_error(error) -> bool:
    """
    Xato YAKUNIYmi (qayta urinish foydasiz) yoki VAQTINCHALIKmi?

    Yakuniy: subtitr o'chirilgan, transkript yo'q, video mavjud emas.
             Proxy almashtirish bularni hech qachon o'zgartirmaydi.

    Vaqtinchalik: IP bloki, tarmoq nosozligi. Boshqa IP yordam berishi mumkin.

    Ilgari ikkalasi bir xil ko'rilardi va subtitri o'chirilgan videoda
    ~22 marta bekorga urinilardi — javob juda kech kelardi.
    """
    name = type(error).__name__
    text = str(error).lower()

    permanent_names = (
        "TranscriptsDisabled",
        "NoTranscriptFound",
        "NoTranscriptAvailable",
        "VideoUnavailable",
        "VideoUnplayable",
    )

    if name in permanent_names:
        return True

    markers = (
        "subtitles are disabled",
        "no transcripts were found",
        "transcripts are disabled",
        "video is no longer available",
        "video unavailable",
        "is unplayable",
    )

    return any(marker in text for marker in markers)


def fetch_with_youtube_transcript_api(video_id: str, proxy_url: str = None):
    """return: (items, permanent_failure)"""

    try:
        if proxy_url:
            http_client = httpx.Client(
                proxy=proxy_url,
                timeout=30.0,
            )
        else:
            http_client = httpx.Client(
                timeout=30.0,
            )

        api = YouTubeTranscriptApi(http_client=http_client)

        transcript_list = api.list(video_id)

        print("AVAILABLE TRANSCRIPTS:")

        for transcript in transcript_list:
            print(
                transcript.language_code,
                transcript.language
            )

        selected = None

        # preferred_order = [
        #     "en",
        #     "ru",
        #     "ar",
        #     "zh",
        #     "ko",
        #     "ja"
        # ]
        
        video_language = detect_video_language(
            video_id,
            proxy_url
        )

        preferred_order = []

        if video_language:
            preferred_order.append(video_language)

        preferred_order += [
            "en",
            "ru",
            "ar",
            "zh",
            "ko",
            "ja"
        ]

        # 1) Avval AUTO-GENERATED subtitle tanlaymiz
        for lang in preferred_order:

            try:
                selected = transcript_list.find_generated_transcript(
                    [lang]
                )

                print(
                    "AUTO SUBTITLE:",
                    selected.language_code
                )

                break

            except Exception:
                pass

        # 2) Auto topilmasa, MANUAL subtitle tanlaymiz
        if selected is None:

            for lang in preferred_order:

                try:
                    selected = transcript_list.find_manually_created_transcript(
                        [lang]
                    )

                    print(
                        "MANUAL SUBTITLE:",
                        selected.language_code
                    )

                    break

                except Exception:
                    pass

        # 3) Baribir topilmasa, birinchi mavjud subtitle
        if selected is None:

            try:
                selected = next(
                    iter(transcript_list)
                )

                print(
                    "FALLBACK SUBTITLE:",
                    selected.language_code
                )

            except Exception:
                # Ro'yxat bo'sh — bu videoda subtitr yo'q. Yakuniy javob.
                return [], True

        raw = selected.fetch()

        try:
            raw = raw.to_raw_data()
        except Exception:
            pass

        items = []

        for item in raw:

            items.append({
                "text": clean_text(
                    item.get("text", "")
                ),
                "start": item.get("start", 0),
                "duration": item.get("duration", 0)
            })

        print(
            "YOUTUBE TRANSCRIPT API OK:",
            len(items)
        )

        return items, False

    except Exception as error:

        permanent = is_permanent_transcript_error(error)

        print(
            "YOUTUBE TRANSCRIPT API ERROR (%s):"
            % ("YAKUNIY" if permanent else "vaqtinchalik"),
            error
        )

        return [], permanent

def fetch_with_whisper(video_id: str):

    try:
        import yt_dlp

        url = f"https://www.youtube.com/watch?v={video_id}"

        temp_dir = tempfile.mkdtemp()

        audio_path = os.path.join(
            temp_dir,
            f"{video_id}.m4a"
        )

        ydl_opts = {
            "format": "bestaudio/best",
            "outtmpl": audio_path,
            "quiet": True,
            "ignoreerrors": True
        }


        proxy_url = get_proxy_url(video_id)

        if proxy_url:
            ydl_opts["proxy"] = proxy_url

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.extract_info(url, download=True)

        if not os.path.exists(audio_path):
            print("AUDIO FILE NOT FOUND")
            return []

        with open(audio_path, "rb") as audio_file:

            transcript = client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file,
                response_format="verbose_json",
                timestamp_granularities=["segment"]
            )

        items = []

        for segment in transcript.segments:

            start = float(segment.start)
            end = float(segment.end)
            text = clean_text(segment.text)

            if not text:
                continue

            items.append({
                "text": text,
                "start": start,
                "duration": end - start
            })

        print("WHISPER TRANSCRIPT OK:", len(items))

        return items

    except Exception as error:

        print("WHISPER TRANSCRIPT ERROR:", error)

        return []    
# TRANSLATE BATCH
# =========================
def translate_with_context(
    current_text: str,
    previous_text: str = "",
    next_text: str = ""
) -> str:

    try:
        response = client.chat.completions.create(
            model=MODEL,
            messages=[
                {
                    "role": "system",
                    "content": (
                        # "You are a professional subtitle translator. "
                        # "Translate ONLY the current subtitle into natural Uzbek Latin. "
                        # "Use previous and next subtitles only for context. "
                        # "Do not translate previous or next subtitle. "
                        # "Do not continue the story. "
                        # "Do not summarize. "
                        # "Return only Uzbek translation."
                        "You are a world-class subtitle translator specializing in English, Russian, Arabic, Chinese, Korean and Japanese to Uzbek Latin.\n"
                        "Your goal is to produce subtitles that sound like they were originally spoken in Uzbek.\n"

                        "Rules:\n"
                        "- Translate ONLY the CURRENT subtitle.\n"
                        "- Previous and next subtitles are ONLY for understanding context.\n"
                        "- NEVER translate previous or next subtitles.\n" 
                        "- NEVER continue the dialogue.\n"
                        "- NEVER summarize.\n"
                        "- NEVER omit information.\n"
                        "- Preserve the exact meaning, tone and emotion.\n"
                        "- Use fluent, natural spoken Uzbek Latin.\n"
                        "- Avoid literal word-for-word translation.\n"
                        "- Adapt idioms and expressions naturally into Uzbek.\n"
                        "- Keep names, brands, places and numbers unchanged.\n"
                        "- Keep Islamic terms accurate (Allah, Qur'on, Rasululloh, etc.).\n"
                        "- Keep technical terms accurate.\n"
                        "- If the sentence is incomplete, translate it as an incomplete subtitle.\n"
                        "- Do not add explanations.\n"
                        "- Do not use quotation marks unless they exist in the original.\n"
                        "- Return ONLY the Uzbek translation."
                    )
                },
                {
                    "role": "user",
                    "content": f"""
Previous:
{previous_text}

Subtitle to translate:
{current_text}

Current:
{current_text}

Next:
{next_text}
Translate ONLY "Subtitle to translate".
"""

                }
            ],
            temperature=0.3
        )

        return clean_text(
            response.choices[0].message.content.strip()
        )

    except Exception as error:
        print("TRANSLATE CONTEXT ERROR:", error)
        return current_text

# =========================


def translate_batch(items):

    def worker(i):

        item = items[i]

        previous_text = ""
        next_text = ""

        if i > 0:
            previous_text = clean_text(
                items[i - 1]["text"]
            )

        if i < len(items) - 1:
            next_text = clean_text(
                items[i + 1]["text"]
            )

        original = clean_text(
            item["text"]
        )

        translated = translate_with_context(
            current_text=original,
            previous_text=previous_text,
            next_text=next_text
        )

        return {
            "index": item["index"],
            "text": original,
            "translated": translated,
            "start": item["start"],
            "duration": item["duration"]
        }

    with ThreadPoolExecutor(
        max_workers=5
    ) as executor:

        result = list(
            executor.map(
                worker,
                range(len(items))
            )
        )

    result.sort(
        key=lambda x: x["index"]
    )

    return result
# TRANSCRIPT API
# =========================
@app.get("/transcript/{video_id}")
def get_transcript(
    video_id: str,
    limit: int = Query(default=40),
    offset: int = Query(default=0),
    nocache: str = Query(default=""),
    authorization: str = Header(default=None)
):

    # REQUIRE_AUTH o'chiq bo'lsa hech kimni rad etmaydi, faqat log yozadi.
    # Yangi ilova tarqalgach Railway'da REQUIRE_AUTH=1 qo'yasiz.
    _uid, blocked = check_access(authorization)

    if blocked == "auth":
        # Eski ilova — 401 ni tushunmaydi, subtitr ko'rinishida xabar beramiz
        return update_notice_subtitles()

    if blocked == "balance":
        # Yangi ilova — {"error": true, ...} shaklini biladi va chiroyli chiqaradi
        return {
            "error": True,
            "message": "Vaqtingiz tugagan. Iltimos, vaqt sotib oling."
        }

    raw_items = fetch_transcript(video_id)

    if (
        len(raw_items) == 1
        and raw_items[0]["text"] == "NO_SUBTITLE_AVAILABLE"
    ):
        return {
            "error": True,
            "message": "Bu videoda subtitle mavjud emas."
    }

    if not raw_items:
        return []

    chunk = raw_items[
        offset:offset + limit
    ]

    if not chunk:
        return []

    prepared = []

    for absolute_index, item in enumerate(
        chunk,
        start=offset
    ):

        prepared.append({
            "index": absolute_index,
            "text": item["text"],
            "start": item["start"],
            "duration": item["duration"]
        })

    # =====================================================================
    # YANGI PIPELINE — env flag bilan yoqiladi/o'chiriladi
    # =====================================================================
    # TRANSLATE_V2=1  -> gap-batch tarjima (sifatli)
    # o'chirilgan     -> eski translate_batch (bugungi holat, o'zgarmagan)
    #
    # Railway'da faqat env o'zgaruvchini almashtirasiz — REDEPLOY KERAK EMAS,
    # muammo bo'lsa bir zumda qaytarasiz.
    #
    # Javob shakli AYNAN bir xil: har bir segment uchun bitta element,
    # o'sha index / start / duration bilan. Faqat `translated` sifatli bo'ladi.
    # Shuning uchun App Store'dagi ilovaga tegish KERAK EMAS.
    if os.getenv("TRANSLATE_V2") in ("1", "true", "yes", "on"):
        try:
            # TRANSLATE_PAIRED=1 -> `text` va `translated` ikkisi ham to'liq gap
            # bo'ladi, ya'ni ekranda ingliz va o'zbek matni DOIM mos keladi.
            # O'chirilgan bo'lsa har segment alohida tarjima qilinadi (moslik
            # buzilishi mumkin, lekin `text` asl segment bo'lagi bo'lib qoladi).
            if os.getenv("TRANSLATE_PAIRED") in ("1", "true", "yes", "on"):
                from translator import translate_range_paired

                return translate_range_paired(
                    raw_items,
                    offset,
                    limit,
                    video_title=""
                )

            from translator import translate_range_strict

            return translate_range_strict(
                raw_items,
                offset,
                limit,
                video_title=""
            )

        except Exception as error:
            # Yangi pipeline qulasa — jim turmaydi, eskisiga qaytadi.
            # Foydalanuvchi hech narsani sezmaydi.
            print("V2 TRANSLATE FAILED, FALLING BACK TO V1:", error)

    return translate_batch(prepared)


@app.get("/video-url/{video_id}")
def get_video_url(video_id: str):
    try:
        import yt_dlp

        cache_key = f"video:{video_id}"

        cached = get_cache(cache_key)
        if cached is not None:
            print("VIDEO URL FROM REDIS")
            return cached

        url = f"https://www.youtube.com/watch?v={video_id}"

        ydl_opts = {
            "format": "18/22/best",
            "quiet": True,
            "ignoreerrors": True,
        }

        proxy_url = get_proxy_url(video_id)

        if proxy_url:
            ydl_opts["proxy"] = proxy_url

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        if not info:
            return {
                "video_url": "",
                "title": "",
                "thumbnail": ""
            }

        result = {
            "video_url": info.get("url", ""),
            "title": info.get("title", ""),
            "thumbnail": info.get("thumbnail", "")
        }

        set_cache(
            cache_key,
            result,
            VIDEO_URL_TTL
        )

        return result

    except Exception as error:
        print("VIDEO URL ERROR:", error)

        return {
            "video_url": "",
            "title": "",
            "thumbnail": ""
        }
    
# WORD TRANSLATION
# =========================

@app.get("/translate-word")
def translate_word(
    word: str,
    authorization: str = Header(default=None)
):

    # So'z tarjimasi massiv emas — bu yerda ham eski ilova tushunadigan
    # shaklda javob beramiz, xato o'rniga xabarni tarjima sifatida.
    _uid, blocked = check_access(authorization)

    if blocked:
        return {
            "word": word,
            "translated": UPDATE_NOTICE_UZ
            if blocked == "auth"
            else "Vaqtingiz tugagan"
        }

    word = clean_text(word)
    cache_key = f"word:{word.strip().lower()}"

    cached = get_cache(cache_key)

    if cached is not None:
        print("WORD FROM REDIS")
        return cached



    if not word:

        return {
            "word": "",
            "translated": ""
        }

    # response = client.chat.completions.create(

    #     model=MODEL,

    #     messages=[

    #         {
    #             "role": "system",
    #             "content":
    #                 "Translate this word into Uzbek. Return only translation."
    #         },

    #         {
    #             "role": "user",
    #             "content": word
    #         }
    #     ],

    #     temperature=0.2
    # )

    response = client.chat.completions.create(

        model=MODEL,

        messages=[

            {
               "role": "system",
               "content": (
                    "You are a professional multilingual dictionary.\n"
                    "Detect the source language automatically.\n"
                    "Translate the given word or short phrase into natural Uzbek Latin.\n"
                    "Always use Uzbek Latin alphabet.\n"
                    "Never use Cyrillic.\n"
                    "Return only the Uzbek translation.\n"
                    "Never explain.\n"
                    "Never add examples.\n"
                    "Never add pronunciation.\n"
                    "Never add punctuation.\n"
                    "If there are multiple meanings, return the most common one."
            )
        },

        {
            "role": "user",
            "content": word
        }
    ],

    temperature=0
)

    translated = (
        response
        .choices[0]
        .message
        .content
        .strip()
    )

    result = {

        "word": word,

        "translated":
             clean_text(translated)
}

    set_cache(
        cache_key,
        result,
        WORD_TTL
)

    return result


# =========================
# V2 ROUTER (yangi tarjima pipeline)
# =========================
# Fayl OXIRIDA ulanadi: routes_v2 main dan fetch_transcript/get_video_url ni
# kech (lazy) import qiladi, shuning uchun aylanma import bo'lmaydi.
#
# Eski endpoint'lar (/process, /transcript) O'ZGARMAGAN va ishlab turadi.
# Yangi: /v2/process, /v2/subtitles, /v2/transcript
#
# Muammo chiqsa — pastdagi ikki qatorni izohga olib qo'ying, tamom.
from routes_v2 import router as v2_router

app.include_router(v2_router)
