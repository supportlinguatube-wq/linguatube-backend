"""
translator.py — LinguaTube tarjima yadrosi
==========================================
Python 3.9+ mos. main.py ga tegmaydi, mustaqil modul.

MUAMMO NIMA EDI:
  1. Har bir kapshn segmenti ALOHIDA tarjima qilinardi. YouTube auto-caption
     gap o'rtasidan kesiladi, shuning uchun model to'liq gapni ko'rmasdi
     -> kontekst yo'qoladi, uslub sun'iy chiqadi.
  2. Batchga o'tilganda model qatorlarni qo'shib/bo'lib yubordi -> chiqish soni
     kirish sonidan farq qildi -> pozitsiya bo'yicha moslashtirish siljidi ->
     subtitr sinxrondan chiqdi.

PRINTSIP:
  Taymkod HECH QACHON modeldan olinmaydi.
      merge (gaplarga) -> translate (ID-mapped JSON) -> rebuild (asl vaqtlardan)
  Model faqat MATN qaytaradi. Vaqt doim asl segmentlardan hisoblanadi,
  shu sabab siljish matematik jihatdan mumkin emas.
"""

import hashlib
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor

try:
    from cache import get_cache, set_cache, TRANSLATION_TTL
except Exception:  # test / standalone rejimi (redis yo'q)
    _MEM = {}
    TRANSLATION_TTL = 60 * 60 * 24 * 30

    def get_cache(key):
        return _MEM.get(key)

    def set_cache(key, value, ttl=None):
        _MEM[key] = value


# ------------------------------------------------------------------ config
# gpt-4.1-mini o'zbek tili uchun sezilarli kuchsiz (low-resource til).
# Gap-batch tufayli so'rov soni ~15x kamaydi, shuning uchun kuchliroq modelga
# o'tish xarajatni deyarli oshirmaydi.
TRANSLATE_MODEL = os.getenv("TRANSLATE_MODEL", "gpt-4.1")

# XARAJAT: system prompt HAR BIR batch bilan qayta yuboriladi (~1200 token).
# BATCH_SIZE kichik bo'lsa o'sha prompt ko'p marta to'lanadi:
#   12 da  -> 504 segment = 42 batch = ~50 000 token faqat prompt uchun
#   28 da  -> 504 segment = 18 batch = ~22 000 token
# Ya'ni batch'ni oshirish sifatga tegmasdan promptning ulushini ~2.3x kamaytiradi.
# Tushib qolgan ID'lar _translate_batch_safe da qayta so'raladi, shuning uchun
# katta batch xavfsiz.
BATCH_SIZE = int(os.getenv("TRANSLATE_BATCH_SIZE", "28"))
MAX_WORKERS = int(os.getenv("TRANSLATE_WORKERS", "4"))      # parallel batch
CONTEXT_LOOKBACK = 2        # oldingi nechta gap kontekst uchun beriladi
# AUTO-CAPTION'DA PUNKTUATSIYA YO'Q.
# "morning ted", "good morning jim how are you" — nuqta umuman bo'lmaydi,
# shuning uchun faqat punktuatsiyaga tayanib bo'lmaydi: gap yopilmasdan
# 12 segmentni yig'ib oladi va ekranda 30 sekundlik blob paydo bo'ladi.
# Shu sababli uch xil chegara bor, qaysi biri birinchi kelsa gap yopiladi.
MAX_SENTENCE_CHARS = 200
MAX_SENTENCE_SEGMENTS = 4   # nuqtasiz kapshnda asosiy tormoz shu
MAX_SENTENCE_SEC = 10.0

# Segment chegarasi kesganda qoldiq bir segment yetim qolishi mumkin:
#   [19] EN "that well. But um"  -> UZ ">> Lekin,"
#   [29] EN "there."             -> UZ "Shu yerda."
# Bunday bo'lak o'zi ma'nosiz. Shundan qisqa bo'lsa oldingi gapga qo'shiladi.
MIN_SENTENCE_CHARS = int(os.getenv("MIN_SENTENCE_CHARS", "28"))
MAX_CUE_SEC = 7.0           # bundan uzoq cue bo'linadi
MAX_JOIN_GAP = 2.0          # segmentlar orasidagi jimlik (s) -> gap tugadi

_client = None

# Token hisobi — xarajatni o'lchash uchun. compare_v1_v2.py shuni o'qiydi.
USAGE = {
    "requests": 0,
    "prompt_tokens": 0,
    "cached_tokens": 0,
    "completion_tokens": 0,
}


def reset_usage():
    for k in USAGE:
        USAGE[k] = 0


def _openai():
    """Lazy client — import paytida API key talab qilinmasin (test uchun)."""
    global _client
    if _client is None:
        from openai import OpenAI
        _client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    return _client


def _clean(text):
    if not isinstance(text, str):
        return ""
    try:
        import ftfy
        text = ftfy.fix_text(text)
    except Exception:
        pass
    return re.sub(r"\s+", " ", text).strip()


# ================================================================
# 1-BOSQICH: SEGMENTLARNI GAPLARGA BIRLASHTIRISH
# ================================================================

_SENT_END = re.compile(u"[.!?…؟。！？][\"'’”)\\]]*$")

# Nuqta segment ICHIDA qolsa ham gapni yopamiz.
# Auto-caption "into uzbek. first we need to" ko'rinishida kesiladi — punktuatsiya
# segment oxirida bo'lmaydi. Faqat _SENT_END ga tayansak gap yopilmaydi va
# MAX_SENTENCE_CHARS ga yetguncha cho'ziladi (test: 13 segment -> 2 gap).
# Kesish nuqtasi segment chegarasida qoladi, shuning uchun "har bir segment
# aynan bitta gapga tegishli" invarianti saqlanadi.
_SENT_MID = re.compile(u"[.!?…؟。！？][\"'’”)\\]]*\\s")
_NOISE = re.compile(
    u"^[\\[(](music|applause|laughter|inaudible|musiqa|karsak|"
    u"музыка|аплодисменты|"
    u"音楽|拍手)[^\\])]*[\\])]$",
    re.IGNORECASE,
)

# Eski V1 bularni tarjima qilardi ([Music] -> [Musiqa]). Yangi pipeline ularni
# gaplardan chiqarib tashlaydi, shuning uchun tarjimasini alohida beramiz —
# aks holda regressiya bo'ladi.
NOISE_UZ = {
    "music": u"[Musiqa]",
    "musiqa": u"[Musiqa]",
    "музыка": u"[Musiqa]",
    "音楽": u"[Musiqa]",
    "applause": u"[Olqishlar]",
    "karsak": u"[Olqishlar]",
    "аплодисменты": u"[Olqishlar]",
    "拍手": u"[Olqishlar]",
    # Kulgi uchun matn emas, emoji — ekranda tabiiyroq ko'rinadi
    "laughter": u"😅😅",
    "laughs": u"😅😅",
    "laughing": u"😅😅",
    "inaudible": u"[Tushunarsiz]",
}


def _noise_uz(text):
    """'[Applause]' -> '[Olqishlar]'. Mos kelmasa None."""
    if not text:
        return None
    inner = text.strip().strip(u"[]()").strip().lower()
    return NOISE_UZ.get(inner)


def merge_into_sentences(items, max_segments=None, max_chars=None, max_sec=None):
    """
    items:  [{"index","text","start","duration"}]  — xom kapshn segmentlari
    return: [{"sid","text","start","end","seg_from","seg_to","segments":[...]}]

    Har bir gap o'zining asl segmentlarini eslab qoladi — keyin taymkod
    aynan shu segmentlardan qayta tiklanadi.

    Chegaralarni chaqiruvchi belgilashi mumkin (paired rejim ularni pastroq
    qo'yadi, chunki matn ekranga sig'ishi kerak).
    """
    if max_segments is None:
        max_segments = MAX_SENTENCE_SEGMENTS
    if max_chars is None:
        max_chars = MAX_SENTENCE_CHARS
    if max_sec is None:
        max_sec = MAX_SENTENCE_SEC

    sentences = []
    buf = []

    def flush():
        # buf va sentences ro'yxatlari joyida o'zgartiriladi (nonlocal kerak emas)
        if not buf:
            return
        text = _clean(" ".join(s["text"] for s in buf))
        if text:
            sentences.append({
                "sid": len(sentences),
                "text": text,
                "start": buf[0]["start"],
                "end": buf[-1]["start"] + (buf[-1]["duration"] or 0),
                "seg_from": buf[0]["index"],
                "seg_to": buf[-1]["index"],
                "segments": [dict(s) for s in buf],
            })
        del buf[:]

    for position, raw in enumerate(items):
        text = _clean(raw.get("text", ""))
        if not text or _NOISE.match(text):
            continue

        seg = {
            "index": raw.get("index", position),
            "text": text,
            "start": float(raw.get("start", 0) or 0),
            "duration": float(raw.get("duration", 0) or 0),
        }

        # Uzoq jimlik = yangi gap boshlandi
        if buf:
            prev_end = buf[-1]["start"] + buf[-1]["duration"]
            if seg["start"] - prev_end > MAX_JOIN_GAP:
                flush()

        buf.append(seg)

        joined_len = sum(len(s["text"]) + 1 for s in buf)
        span = buf[-1]["start"] + buf[-1]["duration"] - buf[0]["start"]

        if (_SENT_END.search(text)
                or _SENT_MID.search(text)
                or len(buf) >= max_segments
                or span >= max_sec
                or joined_len >= max_chars):
            flush()

    flush()
    return _absorb_orphans(sentences, max_segments)


def _absorb_orphans(sentences, max_segments):
    """
    Yetim bo'lakni oldingi gapga qo'shadi.

    Chegara kesganda qoldiq bir segment qolib ketadi va u o'zi ma'nosiz
    bo'ladi ("there." -> "Shu yerda."). Bunday bo'lak oldingi gapga qo'shiladi,
    natijada o'sha gap chegaradan bitta ortiq segment oladi (max_segments + 1).
    Matn sal uzunroq, lekin ekranda yetim qator chiqmaydi.
    """
    if len(sentences) < 2:
        return sentences

    out = [sentences[0]]

    for s in sentences[1:]:
        prev = out[-1]

        prev_end = prev["end"]
        gap = s["start"] - prev_end

        mergeable = (
            len(s["segments"]) == 1
            and len(s["text"]) < MIN_SENTENCE_CHARS
            and len(prev["segments"]) < max_segments + 1
            and gap <= MAX_JOIN_GAP
        )

        if mergeable:
            prev["segments"].extend(s["segments"])
            prev["text"] = _clean(prev["text"] + " " + s["text"])
            prev["end"] = s["end"]
            prev["seg_to"] = s["seg_to"]
        else:
            out.append(s)

    # sid = ro'yxatdagi o'rni. translate_sentences kontekst uchun
    # sentences[sid-2:sid] dan foydalanadi, shuning uchun bu shart.
    for i, s in enumerate(out):
        s["sid"] = i

    return out


# ================================================================
# 2-BOSQICH: ID-MAPPED BATCH TARJIMA
# ================================================================

SYSTEM_PROMPT = """You are a world-class subtitle translator. You translate from \
English, Russian, Arabic, Chinese, Korean or Japanese into UZBEK (Latin script).

You are given CONSECUTIVE SUBTITLE LINES from one video, in order. You can see
the whole passage, so you understand the conversation. But each line appears on
screen ALONE, at its own timestamp.

## OUTPUT CONTRACT (violating this breaks the video player — highest priority)
Return ONLY valid JSON:
{"lines":[{"id":<int>,"uz":"<translation>"}]}
- Return EXACTLY one object per input id, with the SAME id.
- Never merge two lines into one. Never split one line into two.
- Never move words between lines. Line N's translation must correspond to
  line N's own content, not to its neighbours.
- Never add ids, never drop ids.
- EVERY line comes back in Uzbek. Never return the source text unchanged.
  A standalone discourse marker gets its natural Uzbek equivalent:
  so -> "Xo'sh", well -> "Mayli", right -> "To'g'ri", okay -> "Yaxshi",
  oh -> "Ha", what -> "Nima", hi -> "Salom".

## CLAUSE FRAGMENTS ARE NORMAL — TRANSLATE THEM AS FRAGMENTS
Some lines end with a comma instead of a full stop. That means the speaker
was still mid-sentence and the line was cut at a CLAUSE boundary.
- Translate such a line as a clause, ending it with a comma too.
- Do NOT finish the thought for the speaker. Do NOT borrow the ending from
  the next line. Do NOT turn a clause into a complete sentence.
- Consecutive lines must read naturally when placed one after another.

  WRONG (fragment turned into a finished sentence, next line's content pulled in):
    "it came from, uh, it just came from paranoia,"
      -> "Bu paranoyadan kelib chiqqan va men sakkiz kishini intervyu qilganman."
  RIGHT (fragment stays a fragment):
    "it came from, uh, it just came from paranoia,"
      -> "bu, aslida, shunchaki paranoyadan kelib chiqqan,"

A line ending in a full stop is a complete thought — translate it as a complete
sentence. A line ending in a comma is a fragment — keep it a fragment.

## EACH LINE MUST STAND ALONE  (most important quality rule)
The viewer sees one line at a time. A line ending in a full stop must read as a
complete, sensible caption by itself.
- NEVER output a dangling fragment such as "tilayman. Rahmat," or "ona." or
  "uyda Amerika prezidenti bilan". Such a caption is unusable.
- If the source line is a short complete utterance ("thanks mom", "bye dad",
  "what"), translate it as a short complete utterance ("Rahmat, ona",
  "Xayr, dada", "Nima?").
- Auto-generated captions have no punctuation and are cut mid-phrase
  ("the prime minister is in the united" / "states today for talks"). Use the
  neighbouring lines to UNDERSTAND the sentence, then render each line as a
  readable Uzbek clause. Reading the lines in order must flow naturally.
- Do not repeat information already given in an earlier line, and do not pull
  content from the next line.

## NEVER INVENT, NEVER DROP  (hard rule — a wrong subtitle misleads the viewer)
Translate what is there. Nothing more, nothing less.
- Do NOT add facts, numbers, times, names or details that are not in the source.
  WRONG: "interviews are the ones that got them in there"
      -> "Ulardan biri intervyu chiqishidan besh kun o'tib tanlab olindi."
         (invented "five days" — nothing like it in the source)
  RIGHT: "intervyularim ularni o'sha yerga olib kirgan."
- Do NOT silently drop a clause. If the line contains two parts, both appear.
  WRONG: "solution to it. I've spent a lot of time"
      -> "Men bu mavzu haqida ko'p vaqt sarfladim."   (lost "solution to it")
  RIGHT: "...buning yechimi. Men bunga ko'p vaqt sarfladim."
- A line may contain the END of one sentence and the START of another. That is
  normal. Translate both parts in the same order. Do not merge them into one
  smooth sentence and do not discard the shorter part.
- If the source itself is garbled or repeats ("eight out of I think I
  interviewed eight people"), translate it as-is. Do not tidy it up by
  inventing a cleaner meaning.
- If you are unsure what a fragment means, translate it literally but
  grammatically. A plain translation is always better than a confident guess.

## KEEP CONTENT ON ITS OWN LINE  (do not let meaning drift forward)
Each id must carry the meaning of ITS OWN source text. Do not postpone part of
line N into line N+1, and do not borrow from line N+1 into line N. Even though
timestamps are fixed by the player, drifted meaning makes the subtitle appear
one line late.

WRONG (content of line 16 leaked into line 17):
  16 "things get funny and if you are new here" -> "vaziyat kulgili bo'lib ketadi"
  17 "or welcome aboard please subscribe to"    -> "agar siz yangi bo'lsangiz, xush kelibsiz"
RIGHT:
  16 -> "vaziyat kulgili bo'lib ketadi. Agar bu yerda yangi bo'lsangiz,"
  17 -> "xush kelibsiz! Iltimos, kanalga obuna bo'ling"

A line may end mid-thought — that is normal and correct for subtitles. It is
better to end a line mid-thought than to move its content to another line.

## REGISTER: NEVER MIX siz AND sen
Decide the register once per speaker relationship and hold it for the whole
passage. Never mix the two inside a single line.
- Narrator / host talking to the viewer -> always "siz" (siznin, sizga,
  qilasiz, ko'rasiz).
- Characters inside a film talking to each other -> "sen" for family, friends
  and peers; "siz" for strangers, formal or hierarchical situations.

WRONG: "noto'g'ri holating eshitishingizga ta'sir qiladimi"  (holating = sen,
       eshitishingizga = siz — mixed inside one line)
RIGHT: "noto'g'ri holatingiz eshitishingizga ta'sir qiladimi"  (all siz)
   or: "noto'g'ri holating eshitishingga ta'sir qiladimi"      (all sen)

## NAMES: ONE SPELLING, EVERY TIME
Keep personal names, place names and brands in their original Latin spelling
and never transliterate them. If the source is not in Latin script,
transliterate into Latin once and reuse that exact spelling everywhere.
Attach Uzbek case endings to the unchanged name with no apostrophe gymnastics.

WRONG: "Miyaga" in one line and "Mia" in another line of the same passage.
RIGHT: "Mia", "Miaga", "Mianing" — same stem "Mia" throughout.
Also correct: Colin, Amelia, Genovia, San Francisco, Real Life English.

## TRANSLATION RULES
- Uzbek is SOV: the verb goes LAST. Never keep English/Russian word order.
- Never translate word-for-word. Translate the MEANING, then say it the way an
  Uzbek speaker actually would say it out loud.
- Spoken register, not bookish. Short, clean, natural sentences.
- Filler INSIDE a longer line (you know, like, I mean, basically) may be
  dropped rather than translated literally. But if the filler is the WHOLE
  line, translate it — no line may come back empty.
- Keep numbers, code identifiers and units UNCHANGED.
- If the video is a LANGUAGE LESSON and a line quotes an English word or phrase
  as the thing being taught, keep that phrase in English and build the Uzbek
  sentence around it. "expressions like take a turn or a makeover" ->
  "take a turn yoki makeover kabi iboralar". Never translate the taught phrase
  away — the whole point of the lesson is that phrase.
- Latin script ONLY. Never Cyrillic. Use o', g', sh, ch correctly.
- Islamic terms must be exact: Allah, Qur'on, Rasululloh, hadis, sunnat, namoz.
- Technical terms: use the accepted Uzbek/borrowed term, never invent calques.
- Preserve tone and emotion (jokes stay jokes, urgency stays urgent).
- No explanations, no notes, no quotation marks that were not in the source.

## STYLE CALIBRATION (bad -> good)
"Keling, bu haqda gapiraylik"      -> "Endi shuni ko'rib chiqamiz"
"Siz ko'rishingiz mumkinki"        -> "Ko'rib turganingizdek"
"Bu juda muhim narsadir"           -> "Bu juda muhim"
"Men o'ylaymanki bu ishlaydi"      -> "Menimcha, bu ishlaydi"
"Biz ketamiz boshlash uchun"       -> "Boshlaymiz"
"U aytdi men kelaman deb"          -> "U kelishini aytdi"
"Bu narsa qiladi ishni tez"        -> "Bu ishni tezlashtiradi"
"""


# Prompt har o'zgarganda SHUNI oshiring.
# Kesh kaliti faqat matn hash'i bo'lsa, prompt'ni yaxshilaganingizdan keyin
# allaqachon ko'rilgan videolar ESKI tarjimani Redis'dan olib beradi va
# yaxshilanish sezilmaydi (TTL 30 kun!). Versiya kalitga kirsa, prompt
# o'zgarishi keshni avtomatik bekor qiladi.
PROMPT_VERSION = os.getenv("PROMPT_VERSION", "v3")


def _sent_cache_key(text):
    h = hashlib.sha1(text.encode("utf-8")).hexdigest()[:20]
    return "uz:tr:%s:%s" % (PROMPT_VERSION, h)


def _build_user_message(batch, context_text, video_title, glossary):
    parts = []

    if video_title:
        parts.append(
            "VIDEO TITLE (context only, do not translate):\n%s\n" % video_title
        )

    if glossary:
        gl = ", ".join('"%s" -> "%s"' % (k, v)
                       for k, v in list(glossary.items())[:40])
        parts.append("GLOSSARY (use these exact Uzbek terms):\n%s\n" % gl)

    if context_text:
        parts.append(
            "PRECEDING LINES (context only, DO NOT translate, DO NOT return):\n"
            "%s\n" % context_text
        )

    payload = {"lines": [{"id": s["sid"], "text": s["text"]} for s in batch]}
    parts.append(
        "TRANSLATE THESE LINES. Return one object per id, exactly the same ids:\n"
        + json.dumps(payload, ensure_ascii=False)
    )
    return "\n".join(parts)


def _call_model(batch, context_text, video_title, glossary):
    """Bitta so'rov. return: {sid: uz_text} — to'liq bo'lmasligi mumkin."""
    response = _openai().chat.completions.create(
        model=TRANSLATE_MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _build_user_message(
                batch, context_text, video_title, glossary)},
        ],
        temperature=0.3,
        response_format={"type": "json_object"},
    )

    try:
        usage = response.usage
        USAGE["requests"] += 1
        USAGE["prompt_tokens"] += getattr(usage, "prompt_tokens", 0) or 0
        USAGE["completion_tokens"] += getattr(usage, "completion_tokens", 0) or 0
        details = getattr(usage, "prompt_tokens_details", None)
        if details is not None:
            USAGE["cached_tokens"] += getattr(details, "cached_tokens", 0) or 0
    except Exception:
        pass

    data = json.loads(response.choices[0].message.content)

    wanted = set(s["sid"] for s in batch)
    out = {}

    for row in data.get("lines", []):
        if not isinstance(row, dict):
            continue
        try:
            rid = int(row.get("id"))
        except (TypeError, ValueError):
            continue
        if rid not in wanted:          # model o'ylab topgan ID — tashlanadi
            continue
        uz = _clean(row.get("uz") or row.get("text") or "")
        if uz:
            out[rid] = uz

    return out


def _translate_batch_safe(batch, context_text, video_title, glossary):
    """
    Model ba'zi ID'larni tushirib qoldirsa — FAQAT o'shalar qayta so'raladi.
    Pozitsiya bo'yicha zip QILINMAYDI, shuning uchun siljish bo'lmaydi.
    """
    result = {}
    pending = list(batch)

    for attempt in range(3):
        if not pending:
            break
        try:
            result.update(_call_model(pending, context_text, video_title, glossary))
            pending = [s for s in pending if s["sid"] not in result]
        except Exception as error:
            print("TRANSLATE BATCH ERROR (try %d):" % (attempt + 1), error)

    # Oxirgi himoya: qolganini bittalab; u ham bo'lmasa asl matn qoladi
    for s in pending:
        try:
            got = _call_model([s], context_text, video_title, glossary)
            result[s["sid"]] = got.get(s["sid"], s["text"])
        except Exception as error:
            print("TRANSLATE SINGLE ERROR:", error)
            result[s["sid"]] = s["text"]

    return result


def translate_sentences(sentences, video_title="", glossary=None, known=None):
    """sentences -> {sid: uzbek_text}. Redis kesh + parallel batch.

    `known` — allaqachon tarjima qilingan sid'lar (split_and_translate
    kesish bilan birga qaytargan). Ular modelga qayta yuborilmaydi.

    DIQQAT: `sentences` TO'LIQ ro'yxat bo'lishi kerak, filtrlangan emas —
    kontekst `sentences[sid-2:sid]` bilan olinadi, ya'ni sid ro'yxatdagi
    o'rin bilan mos kelishi shart.
    """
    glossary = glossary or {}
    translations = dict(known or {})
    todo = []

    for s in sentences:
        if s["sid"] in translations:
            continue

        cached = get_cache(_sent_cache_key(s["text"]))
        if cached:
            translations[s["sid"]] = cached
        else:
            todo.append(s)

    if not todo:
        print("TRANSLATION: %d/%d keshdan" % (len(translations), len(sentences)))
        return translations

    batches = [todo[i:i + BATCH_SIZE] for i in range(0, len(todo), BATCH_SIZE)]

    def run(batch):
        first = batch[0]["sid"]
        ctx = " ".join(
            x["text"] for x in sentences[max(0, first - CONTEXT_LOOKBACK):first]
        )
        return _translate_batch_safe(batch, ctx, video_title, glossary)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        for got in executor.map(run, batches):
            translations.update(got)

    for s in todo:
        uz = translations.get(s["sid"])
        if uz and uz != s["text"]:
            set_cache(_sent_cache_key(s["text"]), uz, TRANSLATION_TTL)

    print("TRANSLATION: %d gap, %d so'rov" % (len(sentences), len(batches)))
    return translations


# ================================================================
# 3-BOSQICH: CUE'LARNI ASL TAYMKODLARDAN QAYTA QURISH
# ================================================================

def split_proportional(text, weights):
    """
    Tarjimani len(weights) bo'lakka, asl segment uzunliklariga proporsional
    ravishda SO'Z CHEGARASIDA bo'ladi.

    Kafolatlar:
      - natija uzunligi doimo len(weights)
      - so'zlar tartibi va soni saqlanadi
      - so'z bo'lakdan ko'p bo'lsa hech bir bo'lak bo'sh qolmaydi
      - so'z bo'lakdan kam bo'lsa ortiqcha bo'laklar "" qaytadi
        (build_cues ularning vaqtini oldingi cue'ga qo'shadi)
    """
    n = len(weights)
    words = text.split()

    if n <= 1:
        return [text.strip()]
    if not words:
        return [""] * n
    if len(words) <= n:
        return [words[i] if i < len(words) else "" for i in range(n)]

    # KESISH NUQTALARI usuli.
    # Bo'laklar tartiblangan so'z ro'yxatining ketma-ket qismlari, ya'ni butun
    # masala — (n-1) ta kesish nuqtasini tanlash. Shu sababli so'z tartibi
    # buzilishi TUZILMAVIY jihatdan mumkin emas.
    #
    # (Oldingi versiyada bo'sh bo'lakni qo'shnidan "ta'mirlash" mantig'i bor
    #  edi va ketma-ket ikki bo'sh bo'lakda so'zlarni teskari qo'yardi.)
    m = len(words)
    total_w = float(sum(weights)) or 1.0

    cum = []
    acc = 0
    for w in words:
        acc += len(w) + 1
        cum.append(acc)
    total_chars = float(acc)

    cuts = []          # cuts[i] = 0..i bo'laklardagi so'zlar soni
    acc_w = 0.0
    for i in range(n - 1):
        acc_w += weights[i] / total_w
        target = acc_w * total_chars
        j = 0
        while j < m - 1 and cum[j] < target:
            j += 1
        cuts.append(j + 1)

    # Qat'iy o'suvchi qilamiz: har bo'lakka kamida 1 so'z va keyingilarga
    # ham 1 tadan qoladigan qilib chegaralaymiz.
    prev = 0
    for i in range(n - 1):
        lo = prev + 1
        hi = m - (n - 1 - i)
        cuts[i] = max(lo, min(cuts[i], hi))
        prev = cuts[i]

    parts = []
    start = 0
    for i in range(n - 1):
        parts.append(" ".join(words[start:cuts[i]]))
        start = cuts[i]
    parts.append(" ".join(words[start:]))

    return parts


def _normalize_cues(cues):
    """
    Ustma-ust tushgan cue'larni kesadi. YouTube'ning "rolling" kapshnlarida
    segmentlar bir-birini qoplaydi — pleyer uchun bu zarur.
    """
    for i in range(len(cues) - 1):
        end = cues[i]["start"] + cues[i]["duration"]
        nxt = cues[i + 1]["start"]
        if end > nxt:
            cues[i]["duration"] = round(max(0.25, nxt - cues[i]["start"]), 3)
    return cues


def build_cues(sentences, translations, mode="sentence"):
    """
    mode="sentence": bir gap = bir cue. Eng barqaror, o'qish uchun eng qulay.
                     MAX_CUE_SEC dan uzun bo'lsa avtomatik bo'linadi.
    mode="segment":  asl segment cue'lari saqlanadi, tarjima ular ustiga
                     proporsional taqsimlanadi (eski frontend uchun).

    Ikkalasida ham start/duration FAQAT asl segmentlardan olinadi —
    model chiqishi taymkodga TA'SIR QILMAYDI.
    """
    cues = []

    for s in sentences:
        uz = _clean(translations.get(s["sid"]) or "") or s["text"]
        segs = s["segments"]
        dur = max(0.0, s["end"] - s["start"])

        need_split = (mode == "segment") or (dur > MAX_CUE_SEC and len(segs) > 1)

        if not need_split:
            cues.append({
                "index": len(cues),
                "sid": s["sid"],
                "text": s["text"],
                "translated": uz,
                "start": round(s["start"], 3),
                "duration": round(dur, 3),
                "seg_from": s["seg_from"],
                "seg_to": s["seg_to"],
            })
            continue

        weights = [max(1, len(x["text"])) for x in segs]
        parts = split_proportional(uz, weights)

        for seg, part in zip(segs, parts):
            seg_end = seg["start"] + seg["duration"]

            if not part:
                # Bo'sh bo'lak: cue yaratmaymiz, vaqtini oldingi cue'ga qo'shamiz.
                # Matn ekranda qolib turadi, sinxron buzilmaydi.
                if cues and cues[-1]["sid"] == s["sid"]:
                    last = cues[-1]
                    last["duration"] = round(
                        max(last["duration"], seg_end - last["start"]), 3)
                    last["seg_to"] = seg["index"]
                continue

            cues.append({
                "index": len(cues),
                "sid": s["sid"],
                "text": seg["text"],
                "translated": part,
                "start": round(seg["start"], 3),
                "duration": round(seg["duration"], 3),
                "seg_from": seg["index"],
                "seg_to": seg["index"],
            })

    for i, c in enumerate(cues):
        c["index"] = i

    return _normalize_cues(cues)


# ================================================================
# YUQORI DARAJALI API
# ================================================================

def _normalize_items(items):
    out = []
    for i, it in enumerate(items):
        out.append({
            "index": it.get("index", i),
            "text": it.get("text", ""),
            "start": float(it.get("start", 0) or 0),
            "duration": float(it.get("duration", 0) or 0),
        })
    return out


def translate_transcript(items, video_title="", mode="sentence", glossary=None):
    """Xom kapshn segmentlari -> tarjima qilingan, sinxron cue'lar."""
    normalized = _normalize_items(items)
    sentences = merge_into_sentences(normalized)
    if not sentences:
        return []
    translations = translate_sentences(sentences, video_title, glossary)
    return build_cues(sentences, translations, mode)


def slice_by_time(items, from_time, window):
    """
    Vaqt bo'yicha kesish. Index pagination'dan farqli — chegara doim bir
    joyda tushadi, shuning uchun kesh ishlaydi.
    """
    to_time = from_time + window
    out = []
    for i, it in enumerate(items):
        start = float(it.get("start", 0) or 0)
        end = start + float(it.get("duration", 0) or 0)
        if end < from_time or start >= to_time:
            continue
        merged = dict(it)
        merged["index"] = it.get("index", i)
        out.append(merged)
    return out


def translate_segments(items, video_title="", glossary=None):
    """
    SEGMENTLARNI 1:1 TARJIMA QILADI. Birlashtirish ham, bo'lish ham YO'Q.
    return: {segment_index: uzbek_text}

    Nega shunday: gapni birlashtirib, keyin tarjimani segmentlarga bo'lish
    halokatli natija berdi — o'zbekcha SOV bo'lgani uchun bo'laklar segment
    chegarasiga to'g'ri kelmaydi va ekranda "tilayman. Rahmat," kabi ma'nosiz
    parcha chiqadi.

    Model butun oynani KO'RADI (shuning uchun kontekst bor), lekin har bir
    qatorni o'z joyida tarjima qiladi. Sifat ham, sinxron ham saqlanadi.
    """
    glossary = glossary or {}
    normalized = _normalize_items(items)

    result = {}
    todo = []

    for it in normalized:
        # Shovqin teglari modelga yuborilmaydi — lug'atdan olinadi
        noise = _noise_uz(it["text"])
        if noise:
            result[it["index"]] = noise
            continue

        cached = get_cache(_sent_cache_key(it["text"]))
        if cached:
            result[it["index"]] = cached
        else:
            todo.append(it)

    if not todo:
        return result

    # Batch uchun "sid" = segmentning absolyut indeksi
    units = [{"sid": it["index"], "text": it["text"]} for it in todo]
    batches = [units[i:i + BATCH_SIZE] for i in range(0, len(units), BATCH_SIZE)]

    # Kontekst uchun indeksdan matnga tezkor xarita
    text_by_index = dict((it["index"], it["text"]) for it in normalized)
    order = [it["index"] for it in normalized]
    pos_of = dict((idx, p) for p, idx in enumerate(order))

    def run(batch):
        first = batch[0]["sid"]
        p = pos_of.get(first, 0)
        ctx = " / ".join(
            text_by_index[order[k]]
            for k in range(max(0, p - 3), p)
        )
        return _translate_batch_safe(batch, ctx, video_title, glossary)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        for got in executor.map(run, batches):
            result.update(got)

    for it in todo:
        uz = result.get(it["index"])
        if uz and uz != it["text"]:
            set_cache(_sent_cache_key(it["text"]), uz, TRANSLATION_TTL)

    print("TRANSLATION: %d segment, %d so'rov" % (len(normalized), len(batches)))
    return result


def build_strict_cues_from_map(items, by_index):
    """
    Har bir segment uchun aynan BITTA cue. index/start/duration/text
    o'zgarmaydi — javob shakli eski `translate_batch` bilan bir xil.
    """
    cues = []
    for it in _normalize_items(items):
        uz = by_index.get(it["index"]) or _noise_uz(it["text"]) or it["text"]
        cues.append({
            "index": it["index"],
            "text": it["text"],
            "translated": uz,
            "start": it["start"],
            "duration": it["duration"],
        })
    return cues


def build_strict_cues(items, sentences, translations):
    """
    STRICT rejim — App Store'da turgan ilova uchun.

    Kafolat: kirishdagi HAR BIR segment uchun aynan BITTA cue qaytadi,
    o'sha tartibda, o'sha `index`, `start`, `duration` bilan. Ya'ni javob
    tuzilishi eski `translate_batch` bilan farq qilmaydi — faqat `translated`
    maydoni sifatliroq bo'ladi. Frontend'ga tegish kerak emas.

    Gap bir necha segmentga cho'zilgan bo'lsa, tarjima o'sha segmentlarga
    proporsional TAQSIMLANADI.

    (Oldingi versiyada to'liq gap tarjimasi har bir segmentda TAKRORLANARDI.
     Haqiqiy videoda bu halokat bo'ldi: nuqtasiz auto-caption'da gap 12
     segmentni qamrab oldi va bitta uzun paragraf ekranda 30 sekund turdi.)

    `text` maydoni har segmentning o'z asl matni bo'lib qoladi, shuning uchun
    so'z bosib lug'at chiqarish xususiyati avvalgidek ishlaydi.
    """
    by_seg = {}

    for s in sentences:
        uz = _clean(translations.get(s["sid"]) or "") or s["text"]
        segs = s["segments"]

        if len(segs) == 1:
            by_seg[segs[0]["index"]] = uz
            continue

        weights = [max(1, len(x["text"])) for x in segs]
        parts = split_proportional(uz, weights)
        for seg, part in zip(segs, parts):
            by_seg[seg["index"]] = part

    cues = []
    carry = ""   # bo'lak bo'sh chiqsa oldingi matn ekranda qolsin (yaltillamasin)

    for it in _normalize_items(items):
        idx = it["index"]

        uz = by_seg.get(idx)

        if uz is None:
            # Gapga kirmagan segment: [Music]/[Applause] tarjimasi, bo'lmasa asli
            uz = _noise_uz(it["text"]) or it["text"]

        if not uz.strip():
            uz = carry or it["text"]
        else:
            carry = uz

        cues.append({
            "index": idx,
            "text": it["text"],
            "translated": uz,
            "start": it["start"],
            "duration": it["duration"],
        })

    # DIQQAT: strict rejimda _normalize_cues ATAYLAB chaqirilmaydi —
    # taymkodlar eski javob bilan bit-bit bir xil qolishi kerak.
    return cues


# PAIRED rejim chegaralari — env bilan sozlanadi, redeploy kerak emas.
#
# Nega 3, 2 emas: 2 segmentda birlik ko'pincha gap yarmida tugaydi, model esa
# tugallanmagan bo'lakni tarjima qilishga urinib qo'shni cue'dan so'z tortadi
# yoki yo'q narsani to'qib qo'yadi (test'da "besh kun o'tib" — inglizchada yo'q).
# 3 segmentda birlik to'liq fikrga yaqinlashadi va bu ehtiyoj kamayadi.
# Matn uzunroq bo'ladi, shuning uchun UI qutisi cho'zilishi kerak.
PAIRED_MAX_SEGMENTS = int(os.getenv("PAIRED_MAX_SEGMENTS", "3"))
PAIRED_MAX_CHARS = int(os.getenv("PAIRED_MAX_CHARS", "160"))


# PAIRED_SPLIT=0 qo'ysangiz eski birlashtirish qaytadi (nuqtada kesish o'chadi).
# Ya'ni qaytarish uchun uch qavat bor:
#   PAIRED_SPLIT=0        -> faqat yangi kesishni o'chiradi
#   TRANSLATE_PAIRED o'chir -> butun paired rejimni o'chiradi
#   TRANSLATE_V2 o'chir     -> butunlay eski kodga qaytadi
PAIRED_SPLIT = os.getenv("PAIRED_SPLIT", "1") not in ("0", "false", "no", "off")

# Nuqta / undov / so'roq — ortidan bo'shliq yoki matn oxiri kelsa chegara
_BOUNDARY = re.compile(u"[.!?…؟。！？][\"'’”)\\]]*(?=\\s|$)")

# RESTORE_PUNCT=0 -> punktuatsiya tiklash o'chadi, bugungi holat qoladi
RESTORE_PUNCT = os.getenv("RESTORE_PUNCT", "1") not in ("0", "false", "no", "off")

# Ketma-ket ikki nuqta orasidagi eng katta masofa shundan oshsa,
# punktuatsiya tiklanadi.
#
# Oldin bu "o'rtacha zichlik" edi va NOTO'G'RI ishladi: oynada bir nechta
# nuqta bo'lsa o'rtacha yetarli chiqardi, lekin bitta joyda 153 belgi
# davomida nuqta bo'lmasdi — muammo aynan o'sha yerda edi. O'rtacha bilan
# lokal muammoni topib bo'lmaydi.
PUNCT_MAX_RUN = int(os.getenv("PUNCT_MAX_RUN", "110"))


def _longest_unpunctuated_run(text):
    """Ketma-ket ikki gap chegarasi orasidagi eng uzun masofa (belgi)."""
    last = 0
    worst = 0
    for m in _BOUNDARY.finditer(text):
        worst = max(worst, m.end() - last)
        last = m.end()
    return max(worst, len(text) - last)

_WORD_ONLY = re.compile(u"[^\\w']+", re.UNICODE)


def _norm_word(w):
    return _WORD_ONLY.sub("", w).lower()


PUNCT_SYSTEM = """You restore punctuation in raw speech-recognition text.

Return ONLY JSON: {"text":"<the same words, with punctuation>"}

HARD RULES — breaking these makes the output unusable:
- Keep EVERY word exactly as given, in the same order. Never add a word, never
  remove a word, never reorder, never correct spelling, never translate.
- Keep filler and repetition exactly where it is (uh, um, you know, I mean,
  repeated words). Do not tidy the speech up.
- You may ONLY add punctuation ( . , ? ! - ) and change capitalisation.

HOW TO SPLIT:
- Put a full stop wherever a thought is COMPLETE, even if the speaker rambles
  on without pausing. Prefer MORE sentence breaks over fewer.
- Each resulting sentence must stand on its own and read as a complete thought,
  because each one will be shown alone on screen as a subtitle.
- Separate filler and false starts with commas so they do not swallow the
  sentence boundary.
"""


SPLIT_CONTRACT = """You are given the RAW, UNPUNCTUATED word stream of an \
auto-generated subtitle track from one video, in order.

Do THREE things in ONE pass:

1. SEGMENT the stream into natural, self-contained display lines.
   - Cut where a thought is COMPLETE. Prefer more cuts over fewer.
   - If one sentence runs longer than ~25 words, cut it again at a natural
     clause boundary (a comma, "and", "but", "so", "that", "which", "because").
   - NEVER cut in the middle of a phrase. A line must never end on a word like
     "the", "my", "a", "to", "and", "is", "I've".
   - Each line appears on screen ALONE, so it must make sense on its own.

2. RESTORE punctuation and capitalisation inside each line.

3. TRANSLATE each line into UZBEK (Latin script), following the style rules
   given further below.

## OUTPUT CONTRACT (violating this breaks the video player — highest priority)
Return ONLY valid JSON:
{"sentences":[{"en":"<line with punctuation>","uz":"<uzbek translation>"}]}

## ABSOLUTE RULE — the English words are preserved EXACTLY
- Concatenating every "en" in order must reproduce the input word-for-word.
- Do NOT add, remove, reorder, merge, split, correct or paraphrase any word.
- You may ONLY add punctuation marks and change letter case.
- Keep filler, repetition and speech errors exactly where they are
  (uh, um, you know, repeated words). Do not tidy the speech up.

## ABSOLUTE RULE — each translation covers ONLY its own line
- "uz" must translate its own "en" and nothing else.
- Never pull meaning forward from the NEXT line. If the thought continues in
  the next line, let it continue there.
- Never leave part of your own line untranslated.
This matters because "en" and "uz" are shown on screen together, at the same
timestamp. If "uz" describes words the viewer has not heard yet, it is wrong.
"""

SPLIT_SYSTEM = (
    SPLIT_CONTRACT
    + "\n\n"
    + "## TRANSLATION STYLE\n"
      "Everything below describes HOW to translate. Follow all of it.\n"
      "IGNORE any output format mentioned below — the OUTPUT CONTRACT above\n"
      "is the only valid format.\n\n"
    + SYSTEM_PROMPT
)

# SPLIT_TRANSLATE=0 -> eski yo'l qaytadi (nuqta tiklash + mexanik kesish).
SPLIT_TRANSLATE = os.getenv(
    "SPLIT_TRANSLATE", "1"
) not in ("0", "false", "no", "off")


def _build_split_user_message(plain, video_title, glossary):
    parts = []

    if video_title:
        parts.append(
            "VIDEO TITLE (context only, do not translate):\n%s\n" % video_title
        )

    if glossary:
        gl = ", ".join('"%s" -> "%s"' % (k, v)
                       for k, v in list(glossary.items())[:40])
        parts.append("GLOSSARY (use these exact Uzbek terms):\n%s\n" % gl)

    parts.append("RAW SUBTITLE WORDS:\n" + plain)
    return "\n".join(parts)


# Blok uzunligi. Modelning xato darajasi ~1000 so'zga 1 ta, va xato
# EHTIMOLI blok uzunligiga eksponensial bog'liq:
#   700 so'z -> ~50% muvaffaqiyat   (jonli logda kuzatilgani)
#   300 so'z -> ~74%
#   150 so'z -> ~86%
# Blok kichik bo'lsa yiqilish ham LOKAL bo'ladi: butun sahifa emas, faqat
# o'sha bo'lak eski yo'lga tushadi.
SPLIT_BLOCK_WORDS = int(os.getenv("SPLIT_BLOCK_WORDS", "150"))

# Chegarani qidirish oynasi. Blok TASODIFIY joyda kesilmasligi shart —
# aks holda har bir blok chegarasida aynan tuzatmoqchi bo'lgan muammo
# qaytadi (gap o'rtasida uzilish). Shuning uchun ±SLACK oralig'idagi eng
# uzun jimlik tanlanadi.
SPLIT_BLOCK_SLACK = int(os.getenv("SPLIT_BLOCK_SLACK", "40"))

# So'z soni to'g'ri, lekin model ba'zi so'zni o'zgartirgan bo'lsa
# ('cuz -> 'cause, wings -> wing's) — bu ulushdan kam bo'lsa tuzatamiz.
SPLIT_MAX_REPAIR = float(os.getenv("SPLIT_MAX_REPAIR", "0.02"))

_TRAIL_PUNCT = re.compile(r"[^\w']+$", re.UNICODE)


def _split_blocks(words, pause_before):
    """So'zlarni bloklarga bo'ladi, chegarani ENG UZUN jimlikka qo'yadi."""
    blocks = []
    start = 0
    total = len(words)

    while start < total:

        if total - start <= SPLIT_BLOCK_WORDS + SPLIT_BLOCK_SLACK:
            blocks.append((start, total))
            break

        lo = start + max(1, SPLIT_BLOCK_WORDS - SPLIT_BLOCK_SLACK)
        hi = min(total, start + SPLIT_BLOCK_WORDS + SPLIT_BLOCK_SLACK)

        cut = hi
        best = None

        if pause_before:
            for i in range(lo, hi):
                gap = pause_before[i]
                if best is None or gap > best:
                    best = gap
                    cut = i

        blocks.append((start, cut))
        start = cut

    return blocks


def _validate_rows(src_words, rows):
    """
    Model javobini asl so'z oqimi bilan solishtiradi.

    return: (rows, None) yoki (None, sabab)

    So'z SONI to'g'ri bo'lsa moslik 1:1 bo'ladi, shuning uchun farq qilgan
    so'zni asliga qaytarib qo'yish yetarli — butun blokni tashlash shart
    emas. Modelning tinish belgisi saqlanadi, faqat so'zning o'zi tiklanadi.
    """
    got = " ".join(r["en"] for r in rows).split()

    if len(got) != len(src_words):
        return None, "so'z soni farq qildi (%d -> %d)" % (
            len(src_words), len(got))

    fixed = []
    repaired = 0

    for original, produced in zip(src_words, got):

        if _norm_word(original) == _norm_word(produced):
            fixed.append(produced)
            continue

        trail = _TRAIL_PUNCT.search(produced)
        fixed.append(original + (trail.group(0) if trail else ""))
        repaired += 1

    if repaired > max(1, int(len(src_words) * SPLIT_MAX_REPAIR)):
        return None, "juda ko'p so'z o'zgargan (%d/%d)" % (
            repaired, len(src_words))

    if repaired:
        print("SPLIT REPAIRED: %d so'z asliga qaytarildi" % repaired)

        # Tuzatilgan so'zlarni qatorlar shakliga qaytaramiz
        position = 0
        for row in rows:
            n = len(row["en"].split())
            row["en"] = " ".join(fixed[position:position + n])
            position += n

    return rows, None


def _raw_rows(words, pause_before, offset):
    """
    Blok yiqilganda zaxira: so'zlarni jimlik bo'yicha bo'laklarga ajratadi,
    tarjimasiz. Chaqiruvchi ularni eski yo'l bilan tarjima qiladi.

    Muhimi: so'zlar O'ZGARMAYDI, ya'ni umumiy kafolat buzilmaydi.
    """
    rows = []
    start = 0
    total = len(words)
    target = 22          # ekranga sig'adigan uzunlik

    while start < total:

        if total - start <= target + 8:
            rows.append({"en": " ".join(words[start:total]), "uz": ""})
            break

        lo = start + max(1, target - 8)
        hi = min(total, start + target + 8)

        cut = hi
        best = None

        for i in range(lo, hi):
            gap = pause_before[offset + i] if pause_before else 0.0
            if best is None or gap > best:
                best = gap
                cut = i

        rows.append({"en": " ".join(words[start:cut]), "uz": ""})
        start = cut

    return rows


def split_and_translate(words, video_title="", glossary=None,
                        pause_before=None):
    """
    Matnni gaplarga bo'ladi VA tarjima qiladi — BITTA so'rovda.

    NEGA BITTA SO'ROV. Eski tartib: xom matn -> KESISH -> tarjima. Kesish
    tushunishdan oldin bo'lgani uchun tizim gap qayerda tugashini bilmay
    turib kesishga majbur edi. Tinish belgisi yo'q avtomatik subtitrda esa
    kesish 160 belgida mexanik bo'lardi — birlik doim gap o'rtasida uzilib,
    model bo'lak-jumlani ko'rib fikrni o'zicha tugatib qo'yardi.

    Endi chegara va tarjima BITTA qarordan chiqadi, ya'ni inglizcha va
    o'zbekcha bir xil so'zlarni qamrashi strukturaviy jihatdan kafolatlangan.

    BLOKLAB ISHLAYDI. Butun oynani bitta so'rovga berish 700+ so'zda 50%
    yiqilardi. Endi ~150 so'zli bloklar, chegarasi jimlikda, har biri
    ALOHIDA tekshiriladi va parallel ketadi.

    KAFOLAT: har bir blokning "en" lari o'sha blokning asl so'zlari bilan
    aynan solishtiriladi. Yiqilgan blok tarjimasiz qaytadi (chaqiruvchi uni
    eski yo'l bilan tarjima qiladi), qolgan bloklar esa yangi yo'ldan
    o'tadi — ya'ni yiqilish lokal.

    return: [{"en","uz"}] yoki None
    """
    if not words:
        return None

    blocks = _split_blocks(words, pause_before)

    def run(block):
        lo, hi = block
        return _split_one_block(
            words[lo:hi], video_title, glossary, pause_before, lo)

    if len(blocks) == 1:
        results = [run(blocks[0])]
    else:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            results = list(executor.map(run, blocks))

    out = []
    ok_blocks = 0

    for rows in results:
        if rows is None:
            return None
        if any(r["uz"] for r in rows):
            ok_blocks += 1
        out.extend(rows)

    if not out:
        return None

    print("SPLIT+TRANSLATE: %d/%d blok, %d gap, %d so'z"
          % (ok_blocks, len(blocks), len(out), len(words)))

    return out


def _split_one_block(words, video_title, glossary, pause_before, offset):
    """Bitta blok. Yiqilsa tarjimasiz xom qatorlar qaytadi (None emas)."""

    plain = " ".join(words)

    key = "uz:split:%s:%s" % (
        PROMPT_VERSION,
        hashlib.sha1(plain.encode("utf-8")).hexdigest()[:20]
    )

    cached = get_cache(key)
    if cached:
        return cached

    try:
        response = _openai().chat.completions.create(
            model=TRANSLATE_MODEL,
            messages=[
                {"role": "system", "content": SPLIT_SYSTEM},
                {"role": "user", "content": _build_split_user_message(
                    plain, video_title, glossary or {})},
            ],
            temperature=0.2,
            response_format={"type": "json_object"},
        )

        try:
            usage = response.usage
            USAGE["requests"] += 1
            USAGE["prompt_tokens"] += getattr(usage, "prompt_tokens", 0) or 0
            USAGE["completion_tokens"] += getattr(
                usage, "completion_tokens", 0) or 0
            details = getattr(usage, "prompt_tokens_details", None)
            if details is not None:
                USAGE["cached_tokens"] += getattr(
                    details, "cached_tokens", 0) or 0
        except Exception:
            pass

        data = json.loads(response.choices[0].message.content)

    except Exception as error:
        print("SPLIT BLOCK ERROR:", error)
        return _raw_rows(words, pause_before, offset)

    out = []

    for row in data.get("sentences") or []:
        if not isinstance(row, dict):
            continue
        en = _clean(row.get("en") or "")
        uz = _clean(row.get("uz") or "")
        if en:
            out.append({"en": en, "uz": uz or en})

    if not out:
        print("SPLIT REJECTED: bo'sh javob")
        return _raw_rows(words, pause_before, offset)

    # ---- KAFOLAT ----
    out, reason = _validate_rows(words, out)

    if out is None:
        print("SPLIT REJECTED (%d so'zlik blok): %s" % (len(words), reason))
        return _raw_rows(words, pause_before, offset)

    set_cache(key, out, TRANSLATION_TTL)

    # Gap tarjimalari alohida keshga ham tushadi — boshqa sahifada
    # o'sha gap uchrasa qayta so'ralmaydi.
    for s in out:
        set_cache(_sent_cache_key(s["en"]), s["uz"], TRANSLATION_TTL)

    return out


def _units_from_split(pre, owner_of_word, speech):
    """split_and_translate natijasini birliklarga aylantiradi.

    Segment qaysi birlikka tegishli ekani so'zlar KO'PCHILIGI bo'yicha
    hal qilinadi — mavjud mantiq bilan bir xil, ya'ni "har bir segment
    aynan bitta birlikka tegishli" invarianti saqlanadi.
    """
    counts = {}
    position = 0

    for i, s in enumerate(pre):
        n = len(s["en"].split())

        for j in range(position, min(position + n, len(owner_of_word))):
            key = (owner_of_word[j], i)
            counts[key] = counts.get(key, 0) + 1

        position += n

    best = {}
    for (seg_index, unit_i), n in counts.items():
        cur = best.get(seg_index)
        if cur is None or n > cur[1]:
            best[seg_index] = (unit_i, n)

    segs_of_unit = {}
    for it in speech:
        pick = best.get(it["index"])
        if pick is None:
            continue
        segs_of_unit.setdefault(pick[0], []).append(it)

    units = []

    for i, s in enumerate(pre):
        segs = segs_of_unit.get(i, [])

        # Segment tegmagan birlik ekranda ko'rinmaydi — oldingisiga qo'shamiz
        if not segs and units:
            units[-1]["text"] = _clean(units[-1]["text"] + " " + s["en"])
            units[-1]["uz"] = _clean(units[-1]["uz"] + " " + s["uz"])
            continue

        units.append({
            "text": s["en"],
            "uz": s["uz"],
            "segments": segs,
        })

    for i, u in enumerate(units):
        u["sid"] = i
        u["segments"].sort(key=lambda x: x["index"])

    return units


def restore_punctuation(text):
    """
    Nuqtasiz ASR matniga tinish belgilarini qo'yadi. So'zlar o'zgarmasligi
    KAFOLATLANADI — chiqish so'zma-so'z tekshiriladi, farq bo'lsa asl matn
    qaytariladi. Ya'ni yomonlashish mumkin emas.
    """
    if not text.strip():
        return text

    key = "uz:punct:v1:" + hashlib.sha1(text.encode("utf-8")).hexdigest()[:20]
    cached = get_cache(key)
    if cached:
        return cached

    try:
        response = _openai().chat.completions.create(
            model=TRANSLATE_MODEL,
            messages=[
                {"role": "system", "content": PUNCT_SYSTEM},
                {"role": "user", "content": text},
            ],
            temperature=0,
            response_format={"type": "json_object"},
        )

        try:
            usage = response.usage
            USAGE["requests"] += 1
            USAGE["prompt_tokens"] += getattr(usage, "prompt_tokens", 0) or 0
            USAGE["completion_tokens"] += getattr(
                usage, "completion_tokens", 0) or 0
        except Exception:
            pass

        data = json.loads(response.choices[0].message.content)
        out = _clean(data.get("text") or "")

    except Exception as error:
        print("PUNCT RESTORE ERROR:", error)
        return text

    # NAZORAT: so'zlar aynan o'sha bo'lishi shart
    src = text.split()
    dst = out.split()

    if len(src) != len(dst):
        print("PUNCT REJECTED: so'z soni farq qildi (%d -> %d)"
              % (len(src), len(dst)))
        return text

    for a, b in zip(src, dst):
        if _norm_word(a) != _norm_word(b):
            print("PUNCT REJECTED: so'z o'zgardi (%r -> %r)" % (a, b))
            return text

    set_cache(key, out, TRANSLATION_TTL)
    return out


def build_sentence_units(items, max_chars, max_segments,
                         video_title="", glossary=None):
    """
    Matnni NUQTADA kesadi, cue'ni esa segment darajasida qoldiradi.

    Muammo shu edi: nuqta segment o'rtasida bo'lsa, qoldiq ham birlikka
    kirib kelardi ("...vouch for any man again. Uh,") va birlik ma'no
    jihatdan tugallanmasdi. Model esa tugallanmagan fikrni ko'rib keyingi
    gapdan tortib olardi — tarjima oldinga ketardi.

    Endi segmentlar matni uzluksiz qatorga birlashtiriladi, har bir belgi
    qaysi segmentdan kelgani eslab qolinadi, keyin matn nuqtada kesiladi.
    Segment ikki gapga tegib qolsa, belgilar KO'PCHILIGI qaysi gapda bo'lsa
    o'shanisiga biriktiriladi.

    return: [{"sid","text","segments":[...]}]
    """
    speech = [it for it in items
              if it["text"].strip() and not _NOISE.match(it["text"].strip())]
    if not speech:
        return []

    # SO'Z darajasida yig'amiz — punktuatsiya tiklansa ham moslik buzilmasin.
    # (Belgi darajasida yig'sak, qo'shilgan tinish belgilari xaritani suradi.)
    words = []
    owner_of_word = []

    for it in speech:
        for w in it["text"].split():
            words.append(w)
            owner_of_word.append(it["index"])

    if not words:
        return []

    plain = " ".join(words)

    # =================================================================
    # KESISH VA TARJIMA BITTA SO'ROVDA
    # =================================================================
    #
    # Faqat tinish belgisi YO'Q matnda ishlaydi. Punktuatsiyali (odatda
    # qo'lda yozilgan) subtitrda _BOUNDARY chegaralarni o'zi topadi va bu
    # yo'l keraksiz — u yerda inglizcha aks-sado chiqishga qo'shilib
    # so'rovni ~47% qimmatlashtirardi. Nuqtasiz matnda esa aksincha:
    # nuqta tiklash so'rovi o'rniga o'tadi, ya'ni ~10% ARZON tushadi.
    if SPLIT_TRANSLATE and _longest_unpunctuated_run(plain) > PUNCT_MAX_RUN:

        # Har bir so'z OLDIDAGI jimlik. Bloklar aynan shu bo'yicha
        # kesiladi — tasodifiy joyda kesilsa har bir blok chegarasida
        # gap o'rtasida uzilish qaytadi.
        seg_time = {
            it["index"]: (it["start"], it["start"] + it["duration"])
            for it in speech
        }

        pause_before = []
        previous = None

        for seg_index in owner_of_word:

            if previous is None or seg_index == previous:
                pause_before.append(0.0)
            else:
                a = seg_time.get(previous)
                b = seg_time.get(seg_index)
                pause_before.append(b[0] - a[1] if (a and b) else 0.0)

            previous = seg_index

        pre = split_and_translate(
            words, video_title, glossary, pause_before)

        if pre:
            return _units_from_split(pre, owner_of_word, speech)

    # Tinish belgilari yetarlimi? Yetmasa modeldan tiklashni so'raymiz.
    # Punktuatsiyali videolarda bu so'rov UMUMAN ketmaydi.
    if RESTORE_PUNCT:
        run = _longest_unpunctuated_run(plain)

        if run > PUNCT_MAX_RUN:
            print("PUNCT: eng uzun nuqtasiz bo'lak %d belgi (chegara %d)"
                  " -> tiklanmoqda" % (run, PUNCT_MAX_RUN))
            fixed = restore_punctuation(plain)
            fixed_words = fixed.split()

            # restore_punctuation allaqachon tekshiradi, bu ikkinchi to'siq
            if len(fixed_words) == len(words):
                words = fixed_words

    # Endi belgi darajasidagi xarita, so'z egaligidan qurilgan
    chunks = []
    owner = []

    for i, w in enumerate(words):
        if chunks:
            chunks.append(" ")
            owner.append(owner_of_word[i])
        chunks.append(w)
        owner.extend([owner_of_word[i]] * len(w))

    full = "".join(chunks)

    # 1) Nuqtalar bo'yicha kesish nuqtalari
    cuts = [m.end() for m in _BOUNDARY.finditer(full)]
    if not cuts or cuts[-1] < len(full):
        cuts.append(len(full))

    # 2) Oraliqlar. Juda uzun bo'lsa (punktuatsiyasiz kapshn) so'z chegarasida
    #    qo'shimcha kesamiz — bu faqat zaxira to'siq.
    spans = []
    start = 0

    for cut in cuts:
        if cut <= start:
            continue

        while cut - start > max_chars:
            # Nuqta yo'q. Ma'no birligi buzilmasin deb VERGULDA kesamiz —
            # vergul ergash gap chegarasi. Punktuatsiya tiklash bosqichi
            # aynan shu vergullarni ishonchli qilib beradi.
            # Tartib: nuqta -> vergul -> bo'shliq.
            limit = min(start + max_chars, cut)
            brk = -1

            for sep in (", ", "; ", ": ", " — ", " - "):
                p = full.rfind(sep, start, limit)
                if p > brk:
                    # ajratgich CHAP bo'lakda qoladi
                    brk = p + len(sep) - 1

            if brk <= start:
                brk = full.rfind(" ", start, limit)
            if brk <= start:
                brk = limit

            spans.append((start, brk + 1 if full[brk:brk + 1] not in ("", " ")
                          else brk))
            start = brk
            while start < len(full) and full[start] == " ":
                start += 1

        spans.append((start, cut))
        start = cut
        while start < len(full) and full[start] == " ":
            start += 1

    # 3) Har bir segmentni belgilar ko'pchiligiga qarab bir oraliqqa biriktiramiz
    tally = {}
    for i, (a, b) in enumerate(spans):
        for pos in range(a, b):
            key = (owner[pos], i)
            tally[key] = tally.get(key, 0) + 1

    best = {}
    for (seg_index, span_i), n in tally.items():
        cur = best.get(seg_index)
        if cur is None or n > cur[1]:
            best[seg_index] = (span_i, n)

    segs_of_span = {}
    for it in speech:
        pick = best.get(it["index"])
        if pick is None:
            continue
        segs_of_span.setdefault(pick[0], []).append(it)

    # 4) Segment tegmagan oraliqni oldingisiga qo'shib yuboramiz
    units = []
    for i, (a, b) in enumerate(spans):
        text = full[a:b].strip()
        if not text:
            continue
        segs = segs_of_span.get(i, [])

        if not segs and units:
            units[-1]["text"] = _clean(units[-1]["text"] + " " + text)
            continue

        units.append({"text": text, "segments": segs})

    # 5) Yetim dumni oldingi birlikka qo'shamiz.
    #    Vergulda kesganda gapning oxirgi bo'lagi yolg'iz qolishi mumkin
    #    ("administration.", "there.") — u ekranda ma'nosiz, va model uni
    #    ko'rib turib oldingi birlikda fikrni tugatib qo'yadi, natijada
    #    mazmun takrorlanadi.
    final = []
    for u in units:
        too_short = len(u["text"]) < MIN_SENTENCE_CHARS

        if final and (not u["segments"] or too_short):
            final[-1]["text"] = _clean(final[-1]["text"] + " " + u["text"])
            final[-1]["segments"].extend(u["segments"])
            continue

        final.append(u)

    for i, u in enumerate(final):
        u["sid"] = i
        u["segments"].sort(key=lambda s: s["index"])

    return final


def translate_range_paired(items, offset, limit, video_title="",
                           pad=4, glossary=None):
    """
    PAIRED rejim — ingliz va o'zbek matni DOIM bir xil so'zlarni qamraydi.

    Muammo: `text` segment bo'lagi, `translated` esa gap tarjimasi bo'lganda
    ekranda ikkisi mos kelmaydi:
        text        = "on this topic, and I've never seen"
        translated  = "Men bu mavzu haqida ko'p vaqt sarfladim,"
    Sabab lingvistik — o'zbekcha SOV, kesim oxirida, shuning uchun model
    to'ldiruvchini qo'shni qatordan tortib olishga majbur.

    Yechim: IKKISI HAM to'liq gap bo'ladi. Gap 2 segmentga cho'zilgan bo'lsa,
    o'sha 2 cue'da bir xil ingliz-o'zbek juftligi turadi.

    Shartnoma: cue soni, `index`, `start`, `duration` O'ZGARMAYDI.
    O'zgaradigan yagona maydon — `text` (segment bo'lagi -> to'liq gap).
    Bu ataylab, chunki moslikni tiklashning boshqa yo'li yo'q.
    """
    normalized = _normalize_items(items)

    chunk = normalized[offset:offset + limit]
    if not chunk:
        return []

    lo = max(0, offset - pad)
    hi = min(len(normalized), offset + limit + pad)
    window = normalized[lo:hi]

    if PAIRED_SPLIT:
        # Matn nuqtada kesiladi -> har bir birlik ma'no jihatdan tugal
        sentences = build_sentence_units(
            window, PAIRED_MAX_CHARS, PAIRED_MAX_SEGMENTS,
            video_title, glossary)
    else:
        # Eski yo'l: segment darajasida birlashtirish (PAIRED_SPLIT=0)
        sentences = merge_into_sentences(
            window,
            max_segments=PAIRED_MAX_SEGMENTS,
            max_chars=PAIRED_MAX_CHARS,
        )

    if not sentences:
        return build_strict_cues_from_map(chunk, {})

    # Tarjimasi allaqachon bor gaplar — split_and_translate ularni
    # kesish bilan birga qaytargan, ya'ni qayta so'rash shart emas.
    #
    # Ro'yxat FILTRLANMAYDI: translate_sentences kontekstni sid bo'yicha
    # kesib oladi, shuning uchun sid ro'yxatdagi o'rniga teng qolishi kerak.
    known = {s["sid"]: s["uz"] for s in sentences if s.get("uz")}

    translations = translate_sentences(
        sentences, video_title, glossary, known)

    # segment indeksi -> (inglizcha gap, o'zbekcha gap)
    pair_by_seg = {}
    for s in sentences:
        uz = _clean(
            s.get("uz") or translations.get(s["sid"]) or ""
        ) or s["text"]
        for seg in s["segments"]:
            pair_by_seg[seg["index"]] = (s["text"], uz)

    cues = []
    for it in chunk:
        pair = pair_by_seg.get(it["index"])

        if pair is None:
            # Gapga kirmagan segment: [Music] va hokazo
            en = it["text"]
            uz = _noise_uz(it["text"]) or it["text"]
        else:
            en, uz = pair

        cues.append({
            "index": it["index"],
            "text": en,
            "translated": uz,
            "start": it["start"],
            "duration": it["duration"],
        })

    return cues


def translate_range_strict(items, offset, limit, video_title="",
                           pad=6, glossary=None):
    """
    Eski `/transcript?limit=&offset=` uchun to'g'ridan-to'g'ri o'rin bosar.
    `translate_batch(prepared)` ni shu bilan almashtirish mumkin.

    - qaytadigan element soni = so'ralgan chunkdagi segment soni (1:1)
    - `index` absolyut segment indeksi (offset dan boshlanadi)
    - chunk chegarasida gap kesilmasligi uchun oldi/orqadan `pad` segment
      kontekst olinadi, keyin faqat so'ralgan diapazon qaytariladi
    """
    normalized = _normalize_items(items)

    chunk = normalized[offset:offset + limit]
    if not chunk:
        return []

    # Chunk chegarasida kontekst uzilmasligi uchun oldi/orqadan `pad` segment
    lo = max(0, offset - pad)
    hi = min(len(normalized), offset + limit + pad)
    window = normalized[lo:hi]

    by_index = translate_segments(window, video_title, glossary)

    return build_strict_cues_from_map(chunk, by_index)


def translate_range(items, offset, limit, video_title="",
                    mode="segment", pad=6, glossary=None):
    """
    Eski index-pagination frontend uchun. MUHIM: chegarada gap kesilmasligi
    uchun oldi/orqadan `pad` segment qo'shib olib, gaplarga birlashtiramiz,
    keyin faqat so'ralgan diapazonni qaytaramiz. Shu sabab chunk chegarasida
    tarjima sifati tushmaydi.
    """
    normalized = _normalize_items(items)

    lo = max(0, offset - pad)
    hi = min(len(normalized), offset + limit + pad)
    window = normalized[lo:hi]
    if not window:
        return []

    sentences = merge_into_sentences(window)
    if not sentences:
        return []

    translations = translate_sentences(sentences, video_title, glossary)
    cues = build_cues(sentences, translations, mode)

    wanted_lo = offset
    wanted_hi = offset + limit
    cues = [c for c in cues if wanted_lo <= c["seg_from"] < wanted_hi]

    for i, c in enumerate(cues):
        c["index"] = c["seg_from"]

    return cues
