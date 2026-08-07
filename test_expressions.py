"""
Iboralar modulining testi. OpenAI ham, Redis ham kerak emas.

    ./venv/bin/python test_expressions.py
"""

import expressions

# Test uchun flagni majburan yoqamiz (prod'da default o'chiq)
expressions.ENABLED = True

from expressions import find_expressions, attach


# ---- 1) Ingliz: asosiy holat ----
text = "I'm looking forward to meeting you."
found = find_expressions(text)

assert len(found) == 1, "topilmadi yoki ortiqcha: %s" % (found,)

e = found[0]
assert e["lemma"] == "look forward to"
assert e["text"] == "looking forward to"

# ENG MUHIM TEKSHIRUV: oraliq asl matnga aynan to'g'ri kelishi shart.
# Ilova aynan shuni tekshiradi va mos kelmasa iborani tashlab yuboradi.
assert text[e["start"]:e["end"]] == e["text"], \
    "oraliq matnga mos kelmadi: %r != %r" % (text[e["start"]:e["end"]], e["text"])

assert e["meaning"], "o'zbekcha ma'no bo'sh"
print("OK  ingliz: %s -> %s" % (e["text"], e["meaning"]))


# ---- 2) So'z chegarasi: ibora so'z ichidan topilmasin ----
assert find_expressions("He gave upstairs a look.") == [] or all(
    x["lemma"] != "give up" for x in find_expressions("He gave upstairs a look.")
), "'gave up' 'upstairs' ichidan topildi"
print("OK  so'z chegarasi hurmat qilinadi")


# ---- 3) Uzunroq ibora qisqasini bosib ketsin ----
# "get used to" ichida "used to" bor. Uzunrog'i g'olib bo'lishi kerak.
found = find_expressions("I got used to it.")
lemmas = [x["lemma"] for x in found]
assert "get used to" in lemmas, lemmas
assert "used to" not in lemmas, "qisqa ibora uzunning ichidan ham topildi: %s" % lemmas
print("OK  uzun ibora ustunlik qiladi (%s)" % lemmas)


# ---- 4) Rus tili ----
ru = "На самом деле, речь идёт о другом."
found = find_expressions(ru)
lemmas = [x["lemma"] for x in found]

assert "на самом деле" in lemmas, lemmas
for x in found:
    assert ru[x["start"]:x["end"]] == x["text"], "rus: oraliq mos emas"
print("OK  rus: %s" % lemmas)


# ---- 5) Ibora yo'q bo'lsa bo'sh massiv ----
assert find_expressions("Xyz qwerty zzz.") == []
assert find_expressions("") == []
print("OK  ibora yo'q -> bo'sh massiv")


# ---- 6) Oraliqlar ustma-ust tushmasin ----
multi = "I will find out and figure out what to do as soon as possible."
found = multi and find_expressions(multi)
spans = [(x["start"], x["end"]) for x in found]
for i in range(len(spans) - 1):
    assert spans[i][1] <= spans[i + 1][0], "oraliqlar kesishdi: %s" % (spans,)
print("OK  oraliqlar kesishmaydi (%d ta ibora)" % len(found))


# ---- 7) attach(): bir xil matn qayta hisoblanmasin, kalit qo'shilsin ----
cues = [
    {"index": 0, "text": text, "translated": "...", "start": 0.0, "duration": 1.0},
    {"index": 1, "text": text, "translated": "...", "start": 1.0, "duration": 1.0},
]
attach(cues)
assert cues[0]["expressions"] == cues[1]["expressions"]
assert len(cues[0]["expressions"]) == 1
print("OK  attach: takroriy matn bir marta hisoblanadi")


# ---- 8) Flag O'CHIQ bo'lsa kalit UMUMAN qo'shilmasin ----
expressions.ENABLED = False
clean = [{"index": 0, "text": text, "translated": "x",
          "start": 0.0, "duration": 1.0}]
attach(clean)
assert "expressions" not in clean[0], \
    "flag o'chiq, lekin kalit qo'shildi — javob shakli o'zgarib ketdi"
assert find_expressions(text) == []
print("OK  flag o'chiq -> javob bugungisidan farq qilmaydi")

expressions.ENABLED = True

print("\n" + "=" * 50)
print("IBORALAR MODULI: BARCHA TESTLAR O'TDI")
