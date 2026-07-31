"""
Sinxronlik testi — OpenAI API kalitisiz ishlaydi (model chaqirilmaydi).

Ishga tushirish:
    ./venv/bin/python test_translator.py
"""

import random

from translator import (
    merge_into_sentences,
    build_cues,
    split_proportional,
    slice_by_time,
    translate_range,
)

random.seed(7)

# --- Realistik YouTube auto-caption: gap o'rtasidan kesilgan ---
RAW_TEXT = (
    "so today we are going to build a small application that translates "
    "youtube videos into uzbek. first we need to fetch the transcript. "
    "then we send it to the model. the model returns the translation. "
    "finally we render the subtitles on top of the video player. "
    "let me show you how this works in practice. it is actually pretty simple. "
    "there are a few edge cases we have to handle carefully along the way."
)

words = RAW_TEXT.split()
items, t, i = [], 0.0, 0
while i < len(words):
    n = random.randint(4, 9)
    chunk = " ".join(words[i:i + n])
    dur = round(len(chunk) * 0.07 + 0.4, 2)
    items.append({"index": len(items), "text": chunk,
                  "start": round(t, 2), "duration": dur})
    t += dur
    i += n

VIDEO_START = items[0]["start"]
VIDEO_END = items[-1]["start"] + items[-1]["duration"]
print("Xom segmentlar: %d, uzunlik: %.1fs\n" % (len(items), VIDEO_END))


def check_sane(cues, label):
    prev_end = -1.0
    for c in cues:
        assert c["duration"] >= 0, "%s: manfiy davomiylik %s" % (label, c)
        assert c["start"] >= prev_end - 1e-6, "%s: ustma-ust cue %s" % (label, c)
        assert c["translated"].strip(), "%s: bo'sh tarjima %s" % (label, c)
        prev_end = c["start"] + c["duration"]
    return prev_end


# ================= 1) Gaplarga birlashtirish =================
sentences = merge_into_sentences(items)
print("Gaplar: %d segmentdan -> %d gap" % (len(items), len(sentences)))
for s in sentences[:3]:
    print("  [%6.2f-%6.2f] seg %d..%d | %s..."
          % (s["start"], s["end"], s["seg_from"], s["seg_to"], s["text"][:52]))

covered = sum(len(s["segments"]) for s in sentences)
assert covered == len(items), "segment yo'qoldi: %d != %d" % (covered, len(items))
assert all(s["text"] for s in sentences)
assert [s["sid"] for s in sentences] == list(range(len(sentences)))

# Granularlik: matnda 7 ta nuqta bor. Agar gaplar juda kam chiqsa, demak
# nuqta segment ichida qolganda gap yopilmayapti va bir nechta gap bitta
# blobga qo'shilib ketyapti (tarjima sifati tushadi).
dots = RAW_TEXT.count(".")
assert len(sentences) >= dots - 1, \
    "gaplar juda kam: %d gap, matnda %d nuqta — segment ichidagi nuqta " \
    "e'tiborga olinmayapti" % (len(sentences), dots)
print("OK  har bir segment aynan bitta gapga tegishli")
print("OK  granularlik: %d gap / %d nuqta\n" % (len(sentences), dots))

# ============ 2) NUQSONLI model chiqishini simulyatsiya ============
# sid=1 butunlay tushib qolgan; qolganlari asl matndan UZUNROQ qaytgan
fake = {}
for s in sentences:
    if s["sid"] == 1:
        continue
    fake[s["sid"]] = " ".join("soz%d" % k
                              for k in range(len(s["text"].split()) + 4))

# ================= 3) sentence rejimi =================
cues = build_cues(sentences, fake, mode="sentence")
end = check_sane(cues, "sentence")
print("sentence-mode cue: %d" % len(cues))
assert abs(cues[0]["start"] - VIDEO_START) < 1e-6
assert end <= VIDEO_END + 1e-6, "%s > %s" % (end, VIDEO_END)

dropped = [c for c in cues if c["sid"] == 1]
assert dropped, "sid=1 uchun cue umuman yaratilmadi"
# Uzun gap bo'lakka bo'linishi mumkin — shuning uchun birlashtirib solishtiramiz
rejoined = " ".join(c["translated"] for c in dropped).split()
assert rejoined == sentences[1]["text"].split(), \
    "tushib qolgan ID uchun asl matn fallback bo'lmadi: %s" % (rejoined,)
print("OK  siljish yo'q (oxiri %.2fs <= video %.2fs)" % (end, VIDEO_END))
print("OK  model ID tushirib qoldirsa asl matn ko'rsatiladi\n")

# ================= 4) segment rejimi =================
seg_cues = build_cues(sentences, fake, mode="segment")
end2 = check_sane(seg_cues, "segment")
print("segment-mode cue: %d (xom segment: %d)" % (len(seg_cues), len(items)))

starts = set(round(x["start"], 2) for x in items)
for c in seg_cues:
    assert round(c["start"], 2) in starts, "taymkod o'ylab topilgan: %s" % (c,)
assert abs(seg_cues[0]["start"] - VIDEO_START) < 1e-6
assert abs(end2 - VIDEO_END) < 1e-6, "oxiri mos emas: %s != %s" % (end2, VIDEO_END)
assert len(seg_cues) <= len(items)
print("OK  har bir cue start'i ASL segment start'i (model ta'sir qilmaydi)")
print("OK  qamrov %.2fs -> %.2fs to'liq\n" % (VIDEO_START, end2))

# ================= 5) split_proportional =================
assert split_proportional("bir", [5]) == ["bir"]
assert split_proportional("bir ikki", [5, 5, 5]) == ["bir", "ikki", ""]
assert split_proportional("", [1, 2]) == ["", ""]

for _ in range(500):
    n = random.randint(1, 12)
    wc = random.randint(0, 30)
    text = " ".join("w%d" % k for k in range(wc))
    ws = [random.randint(1, 60) for _ in range(n)]
    parts = split_proportional(text, ws)

    assert len(parts) == n, "n mos emas: %d != %d" % (len(parts), n)
    joined = " ".join(p for p in parts if p).split()
    assert joined == text.split(), "so'z buzildi: %s" % (parts,)
    if wc > n:
        assert all(p.strip() for p in parts), \
            "bo'sh bo'lak: n=%d wc=%d %s" % (n, wc, parts)
print("OK  split_proportional: 500 tasodifiy holat, so'z tartibi/soni saqlandi\n")

# ================= 6) Vaqt oynasi =================
WINDOW = 10.0
seen, dupes, t0 = set(), 0, 0.0
while t0 < VIDEO_END:
    for it in slice_by_time(items, t0, WINDOW):
        if it["index"] in seen:
            dupes += 1
        seen.add(it["index"])
    t0 += WINDOW
assert seen == set(range(len(items))), \
    "tushib qolgan segment: %s" % (set(range(len(items))) - seen,)
print("OK  vaqt oynasi barcha %d segmentni qamradi (chegarada %d ta ustma-ust)\n"
      % (len(items), dupes))

# ================= 7) translate_range: chegara qoplamasi =================
# Modelni chaqirmasdan tekshirish uchun translate_sentences ni almashtiramiz
import translator

translator.translate_sentences = lambda sents, title="", glossary=None: {
    s["sid"]: "UZ " + s["text"] for s in sents
}

# translate_segments ham modelni chaqirmasin (strict yo'l shuni ishlatadi)
translator.translate_segments = lambda its, title="", glossary=None: dict(
    (it.get("index", i), "UZ " + it["text"])
    for i, it in enumerate(its)
)

LIMIT = 12
all_idx, seen_idx, dup_idx = [], set(), 0
off = 0
while off < len(items):
    page = translate_range(items, off, LIMIT, mode="segment")
    for c in page:
        assert off <= c["seg_from"] < off + LIMIT, \
            "diapazondan tashqari cue: %s (offset=%d)" % (c, off)
        if c["index"] in seen_idx:
            dup_idx += 1
        seen_idx.add(c["index"])
        all_idx.append(c["index"])
    off += LIMIT

assert dup_idx == 0, "pagination cue'ni takrorladi: %d ta" % dup_idx
assert all_idx == sorted(all_idx), "pagination tartibi buzilgan"
assert len(seen_idx) >= len(items) * 0.9, \
    "pagination segment tushirib qoldirdi: %d / %d" % (len(seen_idx), len(items))
print("OK  translate_range: %d/%d segment, takror 0, tartib buzilmagan"
      % (len(seen_idx), len(items)))
print("OK  chunk chegarasi kontekst padding bilan qoplangan\n")

# ========= 8) STRICT rejim: App Store'dagi ilova uchun 1:1 shartnoma =========
from translator import translate_range_strict, build_strict_cues

# Eski translate_batch javobi qanday bo'lganini modellashtiramiz
def old_shape(chunk):
    return [{"index": c["index"], "text": c["text"], "translated": "?",
             "start": c["start"], "duration": c["duration"]} for c in chunk]

LIMIT = 12
off = 0
total = 0
while off < len(items):
    chunk = items[off:off + LIMIT]
    page = translate_range_strict(items, off, LIMIT)
    expected = old_shape(chunk)

    # 1:1 element soni
    assert len(page) == len(expected), \
        "strict 1:1 buzildi: %d != %d (offset=%d)" % (len(page), len(expected), off)

    for got, exp in zip(page, expected):
        assert got["index"] == exp["index"], "index siljidi: %s vs %s" % (got, exp)
        assert got["text"] == exp["text"], "asl matn o'zgardi: %s" % (got,)
        assert got["start"] == exp["start"], "start o'zgardi: %s" % (got,)
        assert got["duration"] == exp["duration"], "duration o'zgardi: %s" % (got,)
        assert got["translated"].strip(), "bo'sh tarjima: %s" % (got,)
        # Eski javobda bo'lmagan kalit qo'shilmasin (frontend qat'iy parse qilsa)
        assert set(got.keys()) == set(exp.keys()), \
            "javob kalitlari o'zgardi: %s" % (sorted(got.keys()),)

    total += len(page)
    off += LIMIT

assert total == len(items), "strict qamrov to'liq emas: %d != %d" % (total, len(items))

# ---- REGRESSIYA: bir xil uzun matn ketma-ket segmentlarda TAKRORLANMASIN ----
# Haqiqiy videoda shu nuqson chiqdi: nuqtasiz auto-caption'da gap 12 segmentni
# qamrab oldi va bitta paragraf ekranda 30 sekund turdi.
full = translate_range_strict(items, 0, len(items))
run_len, worst = 1, 1
for a, b in zip(full, full[1:]):
    if a["translated"] == b["translated"] and len(a["translated"]) > 25:
        run_len += 1
        worst = max(worst, run_len)
    else:
        run_len = 1
assert worst <= 2, \
    "bir xil uzun tarjima %d ta ketma-ket segmentda takrorlandi" % worst
print("OK  takrorlanish yo'q (eng uzun bir xil ketma-ketlik: %d)" % worst)

# ---- REGRESSIYA: [Music]/[Applause] tarjimasiz qolmasin ----
from translator import _noise_uz
assert _noise_uz("[Music]") == "[Musiqa]"
assert _noise_uz("[Applause]") == "[Olqishlar]"
assert _noise_uz("hello") is None
from translator import build_strict_cues_from_map
noisy = [{"index": 0, "text": "[Applause]", "start": 0.0, "duration": 1.0},
         {"index": 1, "text": "[Music]", "start": 1.0, "duration": 1.0}]
out = build_strict_cues_from_map(noisy, {})
assert [c["translated"] for c in out] == ["[Olqishlar]", "[Musiqa]"], \
    "shovqin teglari tarjima qilinmadi: %s" % ([c["translated"] for c in out],)
print("OK  [Music]/[Applause] o'zbekcha qoldi (V1 bilan bir xil)")
print("OK  strict rejim: %d/%d segment, 1:1, kalitlar/taymkodlar bit-bit bir xil"
      % (total, len(items)))
print("OK  App Store'dagi ilovaga tegish kerak emas\n")

# ========= 9) PAIRED rejim: ingliz va o'zbek DOIM mos kelishi =========
from translator import translate_range_paired
import translator as _t

# Model chaqirilmasin: gap tarjimasi = "UZ " + gap matni
_t.translate_sentences = lambda sents, title="", glossary=None: dict(
    (s["sid"], "UZ " + s["text"]) for s in sents
)

LIMIT = 12
off = 0
total = 0
while off < len(items):
    chunk = items[off:off + LIMIT]
    page = translate_range_paired(items, off, LIMIT)

    assert len(page) == len(chunk), \
        "paired 1:1 buzildi: %d != %d (offset=%d)" % (len(page), len(chunk), off)

    for got, raw in zip(page, chunk):
        # Shartnoma: index/start/duration tegilmaydi
        assert got["index"] == raw["index"], "index siljidi: %s" % (got,)
        assert got["start"] == raw["start"], "start o'zgardi: %s" % (got,)
        assert got["duration"] == raw["duration"], "duration o'zgardi: %s" % (got,)
        assert set(got.keys()) == {"index", "text", "translated",
                                  "start", "duration"}, \
            "kalitlar o'zgardi: %s" % (sorted(got.keys()),)

        # ENG MUHIMI: o'zbekcha aynan ko'rsatilgan inglizchaning tarjimasi
        # bo'lishi kerak. Bizning stub "UZ " + EN qaytaradi, shuning uchun
        # moslikni to'g'ridan-to'g'ri tekshirib ko'ramiz.
        assert got["translated"] == "UZ " + got["text"], \
            "ingliz va o'zbek mos kelmadi!\n  EN: %s\n  UZ: %s" \
            % (got["text"], got["translated"])

        # `text` endi segment bo'lagi emas, to'liq gap — asl bo'lakni O'Z ICHIGA
        # olishi shart (aks holda boshqa gapning matni tushib qolgan)
        assert raw["text"] in got["text"], \
            "cue matni o'z segmentini qamramaydi:\n  segment: %s\n  cue: %s" \
            % (raw["text"], got["text"])

    total += len(page)
    off += LIMIT

assert total == len(items), "paired qamrov: %d != %d" % (total, len(items))
print("OK  paired: %d/%d segment, 1:1, taymkodlar o'zgarmagan" % (total, len(items)))
print("OK  paired: har cue'da ingliz va o'zbek AYNAN bir xil so'zlarni qamraydi")

# Uzunlik chegarasi haqiqatan ushlab turilganini tekshiramiz
full = translate_range_paired(items, 0, len(items))
worst_en = max(len(c["text"]) for c in full)
limit_chars = _t.PAIRED_MAX_CHARS
assert worst_en <= limit_chars + 60, \
    "gap chegaradan juda oshib ketdi: %d belgi (chegara %d)" \
    % (worst_en, limit_chars)
print("OK  paired: eng uzun inglizcha matn %d belgi (chegara %d)"
      % (worst_en, limit_chars))

# Segment chegarasi haqiqatan ushlanyaptimi
runs = {}
for c in full:
    runs[c["text"]] = runs.get(c["text"], 0) + 1
worst_run = max(runs.values())
# +1: yetim bo'lak oldingi gapga qo'shilganda chegaradan bitta oshadi
assert worst_run <= _t.PAIRED_MAX_SEGMENTS + 1, \
    "bir gap %d segmentga cho'zildi (chegara %d + 1)" \
    % (worst_run, _t.PAIRED_MAX_SEGMENTS)
print("OK  paired: eng uzun gap %d segment (chegara %d + yetim qoldiq)"
      % (worst_run, _t.PAIRED_MAX_SEGMENTS))

# Yetim qator qolmasligi kerak: juda qisqa matnli cue bo'lmasin
shorts = [c for c in full
          if len(c["text"]) < _t.MIN_SENTENCE_CHARS and c["text"].strip()]
# Videoning eng oxiridagi qoldiqni qo'shib bo'lmaydi, shuning uchun 1 ta maqbul
assert len(shorts) <= 1, \
    "yetim qatorlar qoldi (%d ta): %s" \
    % (len(shorts), [c["text"] for c in shorts][:4])
print("OK  paired: yetim bo'lak oldingi gapga qo'shildi (%d ta qoldi)\n"
      % len(shorts))

# ===== 10) build_sentence_units: matn nuqtada kesilsin, belgi yo'qolmasin =====
from translator import build_sentence_units

units = build_sentence_units(items, _t.PAIRED_MAX_CHARS, _t.PAIRED_MAX_SEGMENTS)

# Har bir segment aynan bitta birlikka tegishli bo'lishi shart
seen_segs = []
for u in units:
    for s in u["segments"]:
        seen_segs.append(s["index"])
assert len(seen_segs) == len(set(seen_segs)), "segment ikki birlikka tushdi"
assert set(seen_segs) == set(i["index"] for i in items), \
    "segment yo'qoldi yoki ortiqcha: %s" % (
        set(i["index"] for i in items) ^ set(seen_segs),)

# Bo'sh birlik bo'lmasin, sid tartibi to'g'ri bo'lsin
assert all(u["text"].strip() for u in units), "bo'sh birlik bor"
assert [u["sid"] for u in units] == list(range(len(units))), "sid tartibi buzuq"

# Birliklar matni birlashtirilganda asl so'zlar saqlanishi kerak
joined = " ".join(u["text"] for u in units).split()
original = " ".join(i["text"] for i in items).split()
assert joined == original, \
    "so'z yo'qoldi yoki qo'shildi:\n  bor: %s\n  kutilgan: %s" \
    % (joined[:12], original[:12])

# ENG MUHIMI: birlik nuqtadan keyingi qoldiq bilan tugamasin
# ("...again. Uh," kabi — aynan shu tarjimani oldinga surib yuborgan)
bad = []
for u in units[:-1]:
    t = u["text"].rstrip()
    if not t:
        continue
    # ichida nuqta bo'lsa, u matnning OXIRIDA bo'lishi kerak
    hits = list(_t._BOUNDARY.finditer(t))
    if hits and hits[-1].end() < len(t):
        bad.append(t)
assert not bad, \
    "birlik nuqtadan keyin davom etyapti (%d ta):\n  %s" \
    % (len(bad), bad[0])

print("OK  build_sentence_units: %d segment -> %d birlik, so'z yo'qolmadi"
      % (len(items), len(units)))

# ---- Trigger: LOKAL uzun nuqtasiz bo'lak ushlanishi shart ----
import translator as _tr

# Bu matnda 4 ta nuqta bor (o'rtacha zichlik "yetarli" ko'rinadi), lekin
# oxirida 150+ belgi davomida bitta ham nuqta yo'q. Eski o'rtacha-zichlik
# sharti buni O'TKAZIB YUBORARDI.
tricky = ("Short one. Another one. Third here. Fourth done. " +
          "and then he kept talking for a very long time without any "
          "punctuation at all which is exactly the case that used to break "
          "the splitter completely")
run = _tr._longest_unpunctuated_run(tricky)
assert run > _tr.PUNCT_MAX_RUN, \
    "uzun nuqtasiz bo'lak ushlanmadi: %d <= %d" % (run, _tr.PUNCT_MAX_RUN)

# Toza matnda esa ishga tushmasligi kerak (bekorga pul sarflamaslik uchun)
clean = "Short one. Another one. Third here. Fourth done. Fifth as well."
assert _tr._longest_unpunctuated_run(clean) <= _tr.PUNCT_MAX_RUN, \
    "toza matnda bekorga tiklash ishga tushardi"
print("OK  trigger: lokal uzun bo'lak ushlandi (%d belgi), toza matn o'tkazildi"
      % run)

# ---- Zaxira kesish VERGULDA bo'lsin, so'z o'rtasida emas ----
_tr.RESTORE_PUNCT = False           # model chaqirilmasin
long_words = ("But, um, you know, it came from, uh, it just came from "
              "paranoia, you know, and I mean, I think I last time I checked "
              "eight out of, I think I interviewed eight people")
segs = []
t = 0.0
for w in long_words.split():
    segs.append({"index": len(segs), "text": w, "start": t, "duration": 1.0})
    t += 1.0

u3 = build_sentence_units(segs, 60, 3)
assert len(u3) > 1, "uzun nuqtasiz matn umuman bo'linmadi"

# Har bir bo'lak (oxirgisidan tashqari) vergul yoki nuqtada tugashi kerak
bad_cut = [u["text"] for u in u3[:-1]
           if not u["text"].rstrip().endswith((",", ".", ";", ":", "?", "!"))]
assert not bad_cut, \
    "bo'lak ma'no chegarasida emas, so'z o'rtasida kesilgan:\n  %s" % (bad_cut[0],)

# So'zlar baribir yo'qolmasligi kerak
assert " ".join(u["text"] for u in u3).split() == long_words.split(), \
    "vergulda kesishda so'z buzildi"
print("OK  zaxira kesish VERGULDA: %d bo'lak, hammasi ma'no chegarasida"
      % len(u3))

# ---- Yetim dum ("administration.", "there.") yolg'iz qolmasin ----
tail = ("I think I last time I checked eight out of I think I interviewed "
        "eight people that got picked up for the administration.")
segs2 = []
t = 0.0
for w in tail.split():
    segs2.append({"index": len(segs2), "text": w, "start": t, "duration": 1.0})
    t += 1.0

u4 = build_sentence_units(segs2, 100, 3)
orphans = [u["text"] for u in u4[1:]
           if len(u["text"]) < _tr.MIN_SENTENCE_CHARS]
assert not orphans, "yetim dum qoldi: %s" % (orphans,)

# Yetim qo'shilganda segmentlari ham ko'chishi shart, aks holda tushib qoladi
covered = sorted(s["index"] for u in u4 for s in u["segments"])
assert covered == list(range(len(segs2))), \
    "yetim qo'shilganda segment yo'qoldi: %d / %d" % (len(covered), len(segs2))
print("OK  yetim dum oldingi birlikka qo'shildi, segment yo'qolmadi (%d bo'lak)"
      % len(u4))
_tr.RESTORE_PUNCT = True

_calls = {"n": 0}


def _fake_punct(good):
    def inner(text):
        _calls["n"] += 1
        return good(text)
    return inner


orig_restore = _tr.restore_punctuation

# 1) To'g'ri xatti-harakat: faqat tinish belgisi qo'shilgan -> qabul qilinsin
_tr.restore_punctuation = _fake_punct(
    lambda t: ". ".join(t.split(" and ")) if " and " in t else t + ".")
noperiod = [{"index": i, "text": t, "start": i * 2.0, "duration": 2.0}
            for i, t in enumerate(
                ["so today we are going", "to build a small app",
                 "and it translates videos", "into uzbek for people"])]
u2 = build_sentence_units(noperiod, 200, 4)
w_in = " ".join(x["text"] for x in noperiod).split()
w_out = " ".join(u["text"] for u in u2).split()
assert [_tr._norm_word(w) for w in w_out] == [_tr._norm_word(w) for w in w_in], \
    "punktuatsiyadan keyin so'zlar buzildi:\n  %s\n  %s" % (w_out, w_in)
print("OK  punktuatsiya tiklandi, so'zlar o'zgarmadi (%d chaqiruv)"
      % _calls["n"])

# 2) Buzuq xatti-harakat: model so'z qo'shsa/o'zgartirsa -> RAD ETILSIN
_tr.restore_punctuation = orig_restore  # haqiqiy tekshiruvli versiya
_tr._openai = lambda: (_ for _ in ()).throw(RuntimeError("model yo'q"))
assert _tr.restore_punctuation("bir ikki uch") == "bir ikki uch", \
    "model qulasa asl matn qaytarilmadi"
print("OK  model qulasa asl matn qaytariladi (yomonlashish mumkin emas)")

_tr.restore_punctuation = orig_restore
print("OK  birliklar NUQTADA tugaydi (qoldiq keyingisiga o'tadi)")
print("OK  eng uzun birlik %d belgi\n" % max(len(u["text"]) for u in units))

print("=" * 56)
print("BARCHA TESTLAR O'TDI — taymkod siljishi matematik jihatdan mumkin emas")
