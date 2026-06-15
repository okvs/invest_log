"""대시보드 PWA 아이콘 PNG 생성기 (1회 실행, 결과 PNG는 레포에 커밋).

대시보드 배경색(#0f0f14)에 상승 추세 라인(초록 #22c55e)을 그린 단순 아이콘.
재생성: .venv/bin/python bot/pwa/_generate_icons.py
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent
BG = (15, 15, 20, 255)        # #0f0f14
LINE = (34, 197, 94, 255)     # #22c55e
DOT = (240, 255, 245, 255)


def draw_icon(size: int, margin_frac: float, out_path: Path) -> None:
    # 안티에일리어싱: 4배로 그린 뒤 축소
    s = size * 4
    img = Image.new("RGBA", (s, s), BG)
    d = ImageDraw.Draw(img)

    m = s * margin_frac
    box_w = s - 2 * m
    box_h = s - 2 * m
    # 상승 추세 (y는 아래로 증가 → up-trend = y 감소)
    rel = [(0.0, 0.70), (0.26, 0.82), (0.5, 0.52), (0.72, 0.62), (1.0, 0.16)]
    pts = [(m + rx * box_w, m + ry * box_h) for rx, ry in rel]

    w = int(s * 0.055)
    d.line(pts, fill=LINE, width=w, joint="curve")
    r = int(w * 0.95)
    for x, y in pts:
        d.ellipse([x - r, y - r, x + r, y + r], fill=LINE)
    # 끝점 강조 점
    ex, ey = pts[-1]
    rr = int(w * 1.25)
    d.ellipse([ex - rr, ey - rr, ex + rr, ey + rr], fill=DOT)
    # 화살촉 (우상향)
    a = int(s * 0.085)
    d.polygon([(ex, ey - a * 1.1), (ex - a * 0.85, ey + a * 0.2),
               (ex + a * 0.85, ey + a * 0.2)], fill=LINE)

    img = img.resize((size, size), Image.LANCZOS)
    img.save(out_path, "PNG")
    print("wrote", out_path.name, f"{size}x{size}")


def main() -> None:
    draw_icon(512, 0.17, OUT / "icon-512.png")
    draw_icon(192, 0.17, OUT / "icon-192.png")
    draw_icon(180, 0.17, OUT / "apple-touch-icon.png")
    # maskable: 안전영역(중앙 ~80%) 안에 들어오도록 여백 크게
    draw_icon(512, 0.30, OUT / "icon-maskable-512.png")


if __name__ == "__main__":
    main()
