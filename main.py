from fastapi import FastAPI, Query
from youtube_transcript_api import YouTubeTranscriptApi
from openai import OpenAI
from dotenv import load_dotenv
from concurrent.futures import ThreadPoolExecutor
from cache import (
    get_cache,
    set_cache,
    TRANSCRIPT_TTL,
    TRANSLATION_TTL,
    VIDEO_URL_TTL,
    WORD_TTL
)



import os
import json
import re
import ftfy
import subprocess
import tempfile
import asyncio
import httpx
import random
from urllib.parse import urlparse, urlunparse
from importlib.metadata import version

print("youtube-transcript-api version:", version("youtube-transcript-api"))


load_dotenv()

app = FastAPI()

from importlib.metadata import version

@app.get("/version")
def get_version():
    return {
        "youtube_transcript_api": version("youtube-transcript-api")
    }

client = OpenAI(
    api_key=os.getenv("OPENAI_API_KEY")
)

MODEL = "gpt-4.1-mini"


PROXY_URL = os.getenv("YTDLP_PROXY")

if PROXY_URL:
    print("PROXY ENABLED")
else:
    print("NO PROXY")
# =========================

def get_proxy_url():
    if not PROXY_URL:
        return None

    parsed = urlparse(PROXY_URL)

    port = random.randint(10000, 20000)

    netloc = f"{parsed.username}:{parsed.password}@{parsed.hostname}:{port}"

    return urlunparse((
        parsed.scheme,
        netloc,
        parsed.path,
        parsed.params,
        parsed.query,
        parsed.fragment,
    ))
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
    offset: int = Query(default=0)
):

    loop = asyncio.get_running_loop()

    transcript_future = loop.run_in_executor(
        None,
        lambda: get_transcript(
            video_id,
            limit,
            offset,
            ""
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



# =========================
# FETCH TRANSCRIPT
# =========================

def fetch_transcript(video_id: str):

    proxy_url = get_proxy_url()

    cache_key = f"transcript:{video_id}"

    cached = get_cache(cache_key)

    if cached is not None:
        print("TRANSCRIPT FROM REDIS")
        return cached

    if video_id in TRANSCRIPT_CACHE:
        print("TRANSCRIPT FROM MEMORY")
        return TRANSCRIPT_CACHE[video_id]

    # 1) FIRST: youtube-transcript-api
    items = fetch_with_youtube_transcript_api(video_id, proxy_url)

    if items:
        TRANSCRIPT_CACHE[video_id] = items

        set_cache(
           f"transcript:{video_id}",
           items,
           TRANSCRIPT_TTL
)

        return items

    # 2) SECOND: yt-dlp fallback
    items = fetch_with_ytdlp_subtitles(video_id, proxy_url)

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

    return []
# =========================

def fetch_with_ytdlp_subtitles(video_id: str, proxy_url: str = None):

    try:
        import yt_dlp

        url = f"https://www.youtube.com/watch?v={video_id}"

        temp_dir = tempfile.mkdtemp()
        
        available_langs = []

        try:
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
def fetch_with_youtube_transcript_api(video_id: str, proxy_url: str = None):

    try:
        if PROXY_URL:
            http_client = httpx.Client(
                proxy=PROXY_URL,
                timeout=30.0,
            )
            api = YouTubeTranscriptApi(http_client=http_client)
        else:
            api = YouTubeTranscriptApi()

        transcript_list = api.list(video_id)

        print("AVAILABLE TRANSCRIPTS:")

        for transcript in transcript_list:
            print(
                transcript.language_code,
                transcript.language
            )

        selected = None

        preferred_order = [
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
                return []

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

        return items

    except Exception as error:

        print(
            "YOUTUBE TRANSCRIPT API ERROR:",
            error
        )

        return [] 

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


        if PROXY_URL:
            ydl_opts["proxy"] = PROXY_URL

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
                        "You are a professional subtitle translator. "
                        "Translate ONLY the current subtitle into natural Uzbek Latin. "
                        "Use previous and next subtitles only for context. "
                        "Do not translate previous or next subtitle. "
                        "Do not continue the story. "
                        "Do not summarize. "
                        "Return only Uzbek translation."
                    )
                },
                {
                    "role": "user",
                    "content": f"""
Previous:
{previous_text}

Current:
{current_text}

Next:
{next_text}
"""
                }
            ],
            temperature=0.1
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
    nocache: str = Query(default="")
):

    raw_items = fetch_transcript(video_id)

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

        if PROXY_URL:
            ydl_opts["proxy"] = PROXY_URL

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
    word: str
):

    word = clean_text(word)
    cache_key = f"word:{word.lower()}"

    cached = get_cache(cache_key)

    if cached is not None:
        print("WORD FROM REDIS")
        return cached



    if not word:

        return {
            "word": "",
            "translated": ""
        }

    response = client.chat.completions.create(

        model=MODEL,

        messages=[

            {
                "role": "system",
                "content":
                    "Translate this word into Uzbek. Return only translation."
            },

            {
                "role": "user",
                "content": word
            }
        ],

        temperature=0.2
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
