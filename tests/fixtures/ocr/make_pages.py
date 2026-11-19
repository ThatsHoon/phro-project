"""Synthetic scanned pages with known text, for the OCR engine spike.

Writes pages/<name>.png and pages/<name>.txt (ground truth, one line per printed line).
Base pages are drawn at 300 dpi on A4; variants degrade them the way scans differ:
200 and 150 dpi, a 2-degree skew, and noise with JPEG artefacts. The text is written
for this spike; no outside document is copied.

Run: python -I -X utf8 make_pages.py <out_dir>
"""
import io
import zlib
import random
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

KO = [
    "이 문서는 2026년 상반기 사업 계획을 정리한 내부 보고서입니다.",
    "각 부서는 다음 달 10일까지 예산 집행 현황을 제출해야 합니다.",
    "회의는 매주 화요일 오전 9시 30분에 본관 3층 대회의실에서 열립니다.",
    "신규 시스템 도입으로 처리 시간이 평균 42% 줄어들 것으로 예상됩니다.",
    "계약서 제7조에 따라 납품 기한은 서면 합의로만 변경할 수 있습니다.",
    "고객 문의는 전화 02-123-4567 또는 전자우편으로 접수합니다.",
    "현장 점검 결과 안전 장비 일부가 교체 시기를 넘긴 것으로 확인되었습니다.",
    "연구팀은 시제품의 성능 시험을 세 차례 반복하여 결과를 비교하였습니다.",
    "본 자료의 수치는 잠정치이며 최종 결산 후 달라질 수 있습니다.",
    "지원 대상은 공고일 현재 해당 지역에 주소를 둔 중소기업으로 한정합니다.",
    "제출 서류에는 사업자등록증 사본과 최근 2년간의 재무제표가 포함됩니다.",
    "교육 과정은 이론 12시간과 실습 18시간으로 구성되어 있습니다.",
    "위원회는 검토 의견을 반영하여 수정안을 다시 상정하기로 하였습니다.",
    "물품 수령 후 7일 이내에 하자를 통보하지 않으면 검수가 끝난 것으로 봅니다.",
    "올해 매출은 전년 대비 15.8% 늘어난 1,284억 원을 기록하였습니다.",
    "개인정보는 수집 목적이 달성되면 지체 없이 파기합니다.",
]
EN = [
    "This report summarizes the results of the second quarter field survey.",
    "All requests must be submitted through the online portal by March 31.",
    "The committee approved the revised budget of $2.4 million on June 12.",
    "Each sample was measured three times, and the median value is reported.",
    "Shipping costs are calculated by weight and destination at checkout.",
    "Please keep this receipt as proof of purchase for warranty claims.",
    "The new process reduced average handling time from 18 to 11 minutes.",
    "Section 4.2 describes the safety checks required before each test run.",
    "Contact the help desk at extension 4410 if the problem persists.",
    "Payment terms are net 30 days from the date of the invoice.",
    "Figures in this table are rounded to the nearest whole number.",
    "The pilot program will expand to five additional regions next year.",
]

FONTS = {
    "malgun": "C:/Windows/Fonts/malgun.ttf",
    "batang": "C:/Windows/Fonts/batang.ttc",
    "gulim": "C:/Windows/Fonts/gulim.ttc",
    "arial": "C:/Windows/Fonts/arial.ttf",
    "times": "C:/Windows/Fonts/times.ttf",
}
W, H, MARGIN = 2480, 3508, 200  # A4 at 300 dpi


def wrap(draw, text, font, width):
    lines, cur = [], ""
    for word in text.split(" "):
        trial = (cur + " " + word).strip()
        if draw.textlength(trial, font=font) <= width:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


def body_page(sentences, font_path, pt, seed, columns=1):
    rnd = random.Random(seed)
    img = Image.new("L", (W, H), 255)
    d = ImageDraw.Draw(img)
    size = round(pt * 300 / 72)
    font = ImageFont.truetype(font_path, size)
    head = ImageFont.truetype(font_path, round(size * 1.5))
    truth = []
    title = sentences[0].split(" ")[0] + " " + sentences[0].split(" ")[1]
    d.text((MARGIN, MARGIN), title, font=head, fill=0)
    truth.append(title)
    col_w = (W - 2 * MARGIN - (columns - 1) * 80) // columns
    top = MARGIN + round(size * 3)
    lead = round(size * 1.6)
    for c in range(columns):
        x, y = MARGIN + c * (col_w + 80), top
        while y < H - MARGIN - lead * 4:
            para = " ".join(rnd.sample(sentences, 3))
            for line in wrap(d, para, font, col_w):
                if y >= H - MARGIN - lead:
                    break
                d.text((x, y), line, font=font, fill=0)
                truth.append(line)
                y += lead
            y += lead // 2
    return img, truth


def table_page(font_path, seed):
    rnd = random.Random(seed)
    img = Image.new("L", (W, H), 255)
    d = ImageDraw.Draw(img)
    font = ImageFont.truetype(font_path, round(10 * 300 / 72))
    heads = ["구분", "1분기", "2분기", "3분기", "합계"]
    rows = [["매출액"], ["영업이익"], ["인건비"], ["연구개발비"], ["광고비"], ["순이익"]]
    truth = []
    cw, rh, x0, y0 = 400, 110, MARGIN, MARGIN + 100
    grid = [heads]
    for r in rows:
        vals = [rnd.randint(100, 9999) for _ in range(3)]
        grid.append(r + [f"{v:,}" for v in vals] + [f"{sum(vals):,}"])
    for i, row in enumerate(grid):
        for j, cell in enumerate(row):
            x, y = x0 + j * cw, y0 + i * rh
            d.rectangle([x, y, x + cw, y + rh], outline=0, width=3)
            d.text((x + 20, y + 25), cell, font=font, fill=0)
        truth.append(" ".join(row))
    return img, truth


def degrade(img, kind, seed):
    rnd = random.Random(seed)
    if kind == "200dpi":
        return img.resize((W * 2 // 3, H * 2 // 3), Image.LANCZOS)
    if kind == "150dpi":
        return img.resize((W // 2, H // 2), Image.LANCZOS)
    if kind == "skew2":
        return img.rotate(2.0, resample=Image.BICUBIC, expand=False, fillcolor=255)
    if kind == "noisy":
        small = img.resize((W * 2 // 3, H * 2 // 3), Image.LANCZOS).filter(ImageFilter.GaussianBlur(0.8))
        px = small.load()
        w, h = small.size
        for _ in range(w * h // 60):
            x, y = rnd.randrange(w), rnd.randrange(h)
            px[x, y] = rnd.choice((0, 90, 180))
        buf = io.BytesIO()
        small.point(lambda v: 40 + v * 0.75).save(buf, "JPEG", quality=35)
        return Image.open(io.BytesIO(buf.getvalue())).convert("L")
    return img


def main(out):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    bases = {
        "ko-malgun-11": body_page(KO, FONTS["malgun"], 11, 1),
        "ko-batang-10": body_page(KO, FONTS["batang"], 10, 2),
        "ko-gulim-8-2col": body_page(KO, FONTS["gulim"], 8, 3, columns=2),
        "en-times-11": body_page(EN, FONTS["times"], 11, 4),
        "en-arial-9-2col": body_page(EN, FONTS["arial"], 9, 5, columns=2),
        "mix-table-malgun": table_page(FONTS["malgun"], 6),
    }
    for name, (img, truth) in bases.items():
        for kind in ("300dpi", "200dpi", "150dpi", "skew2", "noisy"):
            page = degrade(img, kind, zlib.crc32(name.encode()))
            stem = f"{name}.{kind}"
            page.save(out / f"{stem}.png", optimize=False, compress_level=1)
            (out / f"{stem}.txt").write_text("\n".join(truth) + "\n", encoding="utf-8")
    print(len(bases) * 5, "pages in", out)


if __name__ == "__main__":
    main(sys.argv[1])
