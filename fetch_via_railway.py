"""
fetch_via_railway.py — transkriptni O'Z Railway app'ingiz orqali olish
======================================================================
Mahalliy IP'ingiz YouTube tomonidan bloklangan bo'lsa foydali. Railway'da
proxy sozlangan, shuning uchun u muvaffaqiyatli oladi — biz shunchaki natijani
mahalliy keshga yozib qo'yamiz, keyin compare_v1_v2.py fayldan o'qiydi.

Proxy parolini bilish KERAK EMAS. Faqat app'ingizning ochiq URL'i.

Ishga tushirish:
    ./venv/bin/python fetch_via_railway.py https://sizning-app.up.railway.app FZY1phel0UI
    ./venv/bin/python fetch_via_railway.py https://sizning-app.up.railway.app FZY1phel0UI 80

Uchinchi argument — nechta segment olish (default 80).
"""

import json
import os
import sys
import urllib.request

CACHE_DIR = ".transcript_cache"


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 1

    base = sys.argv[1].rstrip("/")
    video_id = sys.argv[2]
    limit = int(sys.argv[3]) if len(sys.argv) > 3 else 80

    url = "%s/transcript/%s?limit=%d&offset=0" % (base, video_id, limit)
    print("So'rov:", url)

    try:
        with urllib.request.urlopen(url, timeout=180) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception as error:
        print("XATO:", error)
        print("\nURL to'g'rimi? Railway -> Settings -> Domains dan tekshiring.")
        return 1

    if isinstance(data, dict):
        print("XATO: app xato qaytardi:", data.get("message") or data)
        return 1

    if not data:
        print("XATO: bo'sh javob keldi.")
        return 1

    # Bizga faqat XOM transkript kerak: text / start / duration.
    # `translated` maydonini tashlab yuboramiz — u eski V1 tarjimasi,
    # compare skripti tarjimani o'zi qaytadan qiladi.
    items = []
    for row in data:
        items.append({
            "text": row.get("text", ""),
            "start": row.get("start", 0),
            "duration": row.get("duration", 0),
        })

    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, video_id + ".json")

    with open(path, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False)

    print("OK  %d segment saqlandi: %s" % (len(items), path))
    print("\nEndi solishtirishni ishga tushiring:")
    print("  ./venv/bin/python compare_v1_v2.py %s 50" % video_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
