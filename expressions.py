"""
expressions.py — ko'p so'zli iboralarni aniqlash (Multi-word Expressions)
=========================================================================

MUSTAQIL MODUL. Tarjima quvuriga umuman tegmaydi.

Vazifasi bitta: berilgan matnda tanish iborani topib, uning BELGI ORALIG'INI
va o'zbekcha ma'nosini qaytarish. Subtitr matnini, tarjimani, taymkodni yoki
indekslarni o'zgartirmaydi.

NEGA LUG'AT, AI EMAS:
  - nol token, nol xarajat
  - oniy, kechikish yo'q
  - deterministik: bir xil matn har doim bir xil natija
  - xato holati YO'Q, ya'ni "xato bo'lsa bo'sh massiv" talabi o'z-o'zidan
    bajariladi

NEGA SO'Z INDEKSI EMAS, BELGI ORALIG'I:
  So'z indeksi faqat bo'shliq bilan ajraladigan tillarda ishlaydi. Xitoy va
  yapon tillarida bo'shliq yo'q. Ustiga, model/server va ilova so'zlarni bir
  xil sanashi shart — ikki bo'shliq yoki tire bo'lsa moslik buziladi va buni
  sezmay qolasiz.

  Belgi oralig'i universal. Ilova esa `text[start:end] == expression.text`
  ekanini tekshiradi: mos kelmasa iborani e'tiborsiz qoldiradi. Ya'ni
  noto'g'ri ajratib ko'rsatish MUMKIN EMAS.
"""

import json
import os
import re

# Default O'CHIQ. Yoqilmaguncha javobga `expressions` kaliti umuman
# qo'shilmaydi — ya'ni javob bugungisidan belgi-belgi farq qilmaydi.
ENABLED = os.getenv("ENABLE_EXPRESSION_DETECTION") in (
    "1", "true", "yes", "on"
)

_DATA_DIR = os.path.join(os.path.dirname(__file__), "data")

# til kodi -> [(qidiruv_shakli, yozuv), ...]
_INDEX = None

# Kirill harflari bor-yo'qligi bo'yicha tilni ajratamiz. Faqat en/ru uchun
# yetarli; boshqa til qo'shilganda bu joy kengaytiriladi.
_CYRILLIC = re.compile(u"[Ѐ-ӿ]")


def _load():
    """Lug'atlarni bir marta o'qiydi. Fayl yo'q bo'lsa modul jim o'chadi."""
    global _INDEX

    if _INDEX is not None:
        return _INDEX

    _INDEX = {}

    for lang in ("en", "ru"):

        path = os.path.join(_DATA_DIR, "expressions_%s.json" % lang)

        try:
            with open(path, "r", encoding="utf-8") as f:
                entries = json.load(f)

        except Exception as error:
            print("EXPRESSIONS: %s yuklanmadi — %s" % (path, error))
            _INDEX[lang] = []
            continue

        pairs = []

        for entry in entries:

            forms = entry.get("forms") or [entry.get("lemma", "")]

            for form in forms:
                form = form.strip().lower()
                if form:
                    pairs.append((form, entry))

        # Uzun iboralar oldin tekshirilsin: "look forward to" topilsa,
        # ichidagi qisqaroq "look to" ustidan yozib ketmasin.
        pairs.sort(key=lambda p: len(p[0]), reverse=True)

        _INDEX[lang] = pairs

        print("EXPRESSIONS: %s — %d shakl" % (lang, len(pairs)))

    return _INDEX


def _detect_lang(text):
    """Juda sodda: kirill bo'lsa ruscha, aks holda inglizcha."""
    return "ru" if _CYRILLIC.search(text) else "en"


def _norm(text):
    """
    Qidirish uchun normallashtirilgan nusxa.

    Belgilar SONI o'zgarmasligi shart — oraliqlar asl matnga to'g'ri
    kelishi kerak. Shuning uchun faqat kichik harfga o'tkazamiz va
    apostrof variantlarini birxillashtiramiz.
    """
    lowered = text.lower()
    return (
        lowered
        .replace(u"’", "'")
        .replace(u"‘", "'")
    )


def _is_boundary(text, pos):
    """Berilgan o'rin so'z chegarasimi (harf/raqam emasmi)."""
    if pos < 0 or pos >= len(text):
        return True
    return not (text[pos].isalnum() or text[pos] == "'")


def find_expressions(text):
    """
    Matndagi iboralar ro'yxati.

    Har bir element:
        start   — belgi oralig'i boshi (asl matnda)
        end     — oxiri (chegaradan tashqari)
        text    — asl matndagi aynan o'sha bo'lak
        lemma   — lug'atdagi asosiy shakl
        meaning — o'zbekcha ma'no

    HECH QACHON istisno otmaydi. Har qanday muammoda bo'sh ro'yxat.
    """
    if not ENABLED or not text:
        return []

    try:
        index = _load()

        lang = _detect_lang(text)
        pairs = index.get(lang) or []

        if not pairs:
            return []

        haystack = _norm(text)

        found = []
        taken = [False] * len(text)

        for form, entry in pairs:

            start = haystack.find(form)

            while start != -1:

                end = start + len(form)

                # So'z chegarasida bo'lsin: "cut" so'zi "cutlery" ichidan
                # topilmasin
                if (_is_boundary(haystack, start - 1)
                        and _is_boundary(haystack, end)):

                    # Bu joy boshqa (uzunroq) ibora tomonidan band emasmi
                    if not any(taken[start:end]):

                        for i in range(start, end):
                            taken[i] = True

                        found.append({
                            "start": start,
                            "end": end,
                            "text": text[start:end],
                            "lemma": entry.get("lemma", form),
                            "meaning": entry.get("uz", ""),
                        })

                start = haystack.find(form, start + 1)

        found.sort(key=lambda e: e["start"])

        return found

    except Exception as error:
        # Modul hech qachon subtitrni to'xtatmaydi
        print("EXPRESSIONS ERROR:", error)
        return []


def attach(cues):
    """
    Cue ro'yxatiga `expressions` maydonini qo'shadi.

    Flag o'chiq bo'lsa cue'larga UMUMAN TEGMAYDI — kalit ham qo'shilmaydi,
    ya'ni javob bugungisidan farq qilmaydi.

    Bir xil matn takrorlansa (paired rejimda 3-4 cue bir xil matnga ega)
    natija qayta hisoblanmaydi.
    """
    if not ENABLED:
        return cues

    try:
        memo = {}

        for cue in cues:

            text = cue.get("text") or ""

            if text not in memo:
                memo[text] = find_expressions(text)

            cue["expressions"] = memo[text]

    except Exception as error:
        print("EXPRESSIONS ATTACH ERROR:", error)

    return cues
