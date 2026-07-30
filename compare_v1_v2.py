"""
compare_v1_v2.py — eski va yangi tarjimani yonma-yon solishtirish
=================================================================
Haqiqiy video ustida ishlaydi va IKKI narsani tekshiradi:

  1. SHARTNOMA: index / start / duration / kalitlar bit-bit bir xilmi?
     (App Store'dagi ilova buzilmasligi uchun shu majburiy)
  2. SIFAT: o'zbekcha tarjimani ko'z bilan solishtirish

Ishga tushirish:
    ./venv/bin/python compare_v1_v2.py VIDEO_ID
    ./venv/bin/python compare_v1_v2.py VIDEO_ID 20      # limit

DIQQAT: ikki yo'l ham OpenAI'ga so'rov yuboradi, ya'ni pul ketadi.
limit=10 da bu bir necha sentdan oshmaydi.
"""

import json
import os
import sys
import time

from main import fetch_transcript, translate_batch
from translator import translate_range_strict

# Transkriptni faylga saqlaymiz. Sifatni sozlash uchun skriptni ko'p marta
# ishlatasiz — YouTube'ni har safar urish rate-limit'ga (IP ban) olib keladi.
# Bir marta olindi, keyin fayldan o'qiladi.
CACHE_DIR = ".transcript_cache"


def load_transcript(video_id, refresh=False):
    path = os.path.join(CACHE_DIR, video_id + ".json")

    if not refresh and os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            items = json.load(f)
        print("Transkript FAYLDAN o'qildi: %s (%d segment)" % (path, len(items)))
        return items

    items = fetch_transcript(video_id)

    if not items or items[0].get("text") == "NO_SUBTITLE_AVAILABLE":
        return items

    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False)
        print("Transkript saqlandi: %s" % path)
    except Exception as error:
        print("Saqlash xatosi (muhim emas):", error)

    return items


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1

    video_id = sys.argv[1]
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 10
    offset = 0

    print("=" * 78)
    print("VIDEO:", video_id, "| limit:", limit)
    print("=" * 78)

    refresh = "--refresh" in sys.argv
    raw = load_transcript(video_id, refresh=refresh)

    if not raw or raw[0].get("text") == "NO_SUBTITLE_AVAILABLE":
        print("\nXATO: transkript olinmadi.")
        print("Sabab odatda YouTube IP bloki (ko'p so'rov). Yechim:")
        print("  1) Railway -> Variables -> YTDLP_PROXY qiymatini")
        print("     mahalliy .env fayliga ko'chiring, keyin qayta ishlatib ko'ring")
        print("  2) yoki 10-15 daqiqa kutib, qaytadan urinib ko'ring")
        return 1

    print("Xom segmentlar:", len(raw))

    chunk = raw[offset:offset + limit]
    prepared = [
        {
            "index": offset + i,
            "text": it["text"],
            "start": it["start"],
            "duration": it["duration"],
        }
        for i, it in enumerate(chunk)
    ]

    # ---------------- V1 (eski yo'l) ----------------
    t0 = time.time()
    v1 = translate_batch(prepared)
    v1_time = time.time() - t0
    print("\nV1 (segment-bir-so'rov): %.1fs, %d ta so'rov"
          % (v1_time, len(prepared)))

    # ---------------- V2 (yangi yo'l) ----------------
    t0 = time.time()
    v2 = translate_range_strict(raw, offset, limit)
    v2_time = time.time() - t0
    print("V2 (gap-batch):          %.1fs" % v2_time)

    # ================= 1) SHARTNOMA TEKSHIRUVI =================
    print("\n" + "=" * 78)
    print("SHARTNOMA TEKSHIRUVI")
    print("=" * 78)

    problems = []

    if len(v1) != len(v2):
        problems.append("element soni farq qiladi: v1=%d v2=%d" % (len(v1), len(v2)))
    else:
        for a, b in zip(v1, v2):
            if set(a.keys()) != set(b.keys()):
                problems.append("kalitlar farq qiladi: %s vs %s"
                                % (sorted(a.keys()), sorted(b.keys())))
                break
            for field in ("index", "start", "duration", "text"):
                if a.get(field) != b.get(field):
                    problems.append("%s farq qiladi (index=%s): %r vs %r"
                                    % (field, a.get("index"), a.get(field),
                                       b.get(field)))

    if problems:
        print("XATO — App Store'dagi ilova buzilishi mumkin:")
        for p in problems[:10]:
            print("  -", p)
        print("\nDEPLOY QILMANG. Shu xatoni menga tashlang.")
        return 1

    print("OK  element soni:  %d == %d" % (len(v1), len(v2)))
    print("OK  kalitlar:      %s" % sorted(v1[0].keys()))
    print("OK  index / start / duration / text — bit-bit bir xil")
    print("OK  frontend'ga tegish kerak emas")

    # ================= 2) SIFAT SOLISHTIRUVI =================
    print("\n" + "=" * 78)
    print("TARJIMA SIFATI — o'zingiz baholang")
    print("=" * 78)

    changed = 0
    for a, b in zip(v1, v2):
        same = a["translated"].strip() == b["translated"].strip()
        if not same:
            changed += 1
        print("\n[%s]  %.2fs" % (a["index"], a["start"]))
        print("  ASL : %s" % a["text"])
        print("  V1  : %s" % a["translated"])
        print("  V2  : %s%s" % (b["translated"], "   (bir xil)" if same else ""))

    print("\n" + "=" * 78)
    print("%d / %d qatorda tarjima o'zgardi" % (changed, len(v1)))
    print("Tezlik: V1 %.1fs -> V2 %.1fs" % (v1_time, v2_time))
    print("=" * 78)
    print("\nV2 sifati yaxshiroq bo'lsa: Railway -> Variables -> TRANSLATE_V2=1")
    return 0


if __name__ == "__main__":
    sys.exit(main())
