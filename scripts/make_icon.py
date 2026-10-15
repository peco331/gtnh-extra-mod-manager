"""生成程序图标：深色圆角底板、琥珀色齿轮，前景为 Wiki「额外模组」的粉色像素扳手。

用法：py -3.10 scripts/make_icon.py
输出 gtnhmod/assets/icon.png（窗口图标）与 icon.ico（EXE 图标，含 16~256 多尺寸）。
"""
import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

S = 1024  # 高分辨率绘制后缩放，边缘更平滑
OUT = Path(__file__).resolve().parent.parent / "gtnhmod" / "assets"


def gear_polygon(cx, cy, r_out, r_in, teeth, tooth_frac=0.5, steps=24):
    """齿顶略收窄的梯形齿轮外廓。"""
    pts = []
    pitch = 2 * math.pi / teeth
    top = pitch * tooth_frac * 0.75
    base = pitch * tooth_frac
    for i in range(teeth):
        a = i * pitch
        pts += [(a - base / 2, r_in), (a - top / 2, r_out), (a + top / 2, r_out), (a + base / 2, r_in)]
        # 齿间沿内圆走弧线
        for k in range(1, steps):
            t = a + base / 2 + (pitch - base) * k / steps
            pts.append((t, r_in))
    return [(cx + r * math.cos(t - math.pi / 2), cy + r * math.sin(t - math.pi / 2)) for t, r in pts]


def vertical_gradient(size, top, bottom):
    img = Image.new("RGBA", (size, size))
    draw = ImageDraw.Draw(img)
    for y in range(size):
        f = y / (size - 1)
        draw.line([(0, y), (size, y)], fill=tuple(int(a + (b - a) * f) for a, b in zip(top, bottom)) + (255,))
    return img


def build():
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    margin, radius = 40, 200
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle([margin, margin, S - margin, S - margin], radius, fill=255)
    img.paste(vertical_gradient(S, (38, 58, 80), (18, 28, 40)), (0, 0), mask)

    # 齿轮投影
    cx, cy = S * 0.47, S * 0.47
    shadow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).polygon(gear_polygon(cx + 14, cy + 22, 360, 290, 10), fill=(0, 0, 0, 140))
    img.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(18)))

    d = ImageDraw.Draw(img)
    d.polygon(gear_polygon(cx, cy, 360, 290, 10), fill=(176, 106, 26))
    d.polygon(gear_polygon(cx, cy, 340, 274, 10), fill=(244, 170, 58))
    d.ellipse([cx - 230, cy - 230, cx + 230, cy + 230], fill=(255, 196, 94))
    # 中心孔透出底板
    d.ellipse([cx - 120, cy - 120, cx + 120, cy + 120], fill=(176, 106, 26))
    d.ellipse([cx - 100, cy - 100, cx + 100, cy + 100], fill=(30, 46, 64))

    # 前景：GTNH 中文 Wiki「额外模组」入口的粉色像素扳手
    draw_pixel_wrench(img, S * 0.56, S * 0.54, cell=46)
    return img


WRENCH = [
    "..##....##..",
    ".#hp#..#hp#.",
    ".#hp#..#hp#.",
    ".#hp#..#hp#.",
    ".#hpp##ppp#.",
    "..#hppppp#..",
    "...#hppp#...",
    "....#hp#....",
    "....#hp#....",
    "....#hp#....",
    "....#hp#....",
    "....#hp#....",
    "....#hp#....",
    "....#hp#....",
    "....#hp#....",
    ".....##.....",
]
WRENCH_COLORS = {"#": (150, 40, 72), "p": (240, 128, 160), "h": (255, 190, 208)}


def draw_pixel_wrench(img, cx, cy, cell):
    """按像素格绘制，保持 Minecraft 式硬边；先画一层模糊投影。"""
    w, h = len(WRENCH[0]) * cell, len(WRENCH) * cell
    left, top = int(cx - w / 2), int(cy - h / 2)
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    shadow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d, sd = ImageDraw.Draw(layer), ImageDraw.Draw(shadow)
    for r, row in enumerate(WRENCH):
        for c, ch in enumerate(row):
            if ch == ".":
                continue
            box = [left + c * cell, top + r * cell, left + (c + 1) * cell - 1, top + (r + 1) * cell - 1]
            d.rectangle(box, fill=WRENCH_COLORS[ch])
            sd.rectangle([box[0] + 18, box[1] + 24, box[2] + 18, box[3] + 24], fill=(0, 0, 0, 150))
    img.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(14)))
    img.alpha_composite(layer)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    big = build()
    big.resize((256, 256), Image.LANCZOS).save(OUT / "icon.png")
    big.save(OUT / "icon.ico", sizes=[(n, n) for n in (16, 20, 24, 32, 40, 48, 64, 128, 256)])
    print("written", OUT / "icon.png", OUT / "icon.ico")


if __name__ == "__main__":
    main()
