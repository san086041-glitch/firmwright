"""生成应用图标（W8 打包）：和界面里的 Logo 组件同一个图形（32×32 视框：圆角方块 + F 笔画 + 两侧引脚）。
在 1024 px 上画再缩小，输出 icon.png（512）、icon.ico（16–256 多尺寸）和 README 用的 logo.svg。
用法：core\\.venv\\Scripts\\python desktop\\build\\make_icon.py
"""

from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).parent
ACCENT = (61, 214, 163, 255)  # --accent  #3dd6a3
ON_ACCENT = (6, 40, 28, 255)  # --on-accent #06281c
S = 1024 / 32  # 视框 → 像素


def line(d: ImageDraw.ImageDraw, pts, width: float, color) -> None:
    """圆头圆角的折线：线段 + 每个顶点一个圆。"""
    w = width * S
    px = [(x * S, y * S) for x, y in pts]
    d.line(px, fill=color, width=round(w), joint="curve")
    for x, y in px:
        d.ellipse((x - w / 2, y - w / 2, x + w / 2, y + w / 2), fill=color)


def draw() -> Image.Image:
    img = Image.new("RGBA", (1024, 1024), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((4 * S, 4 * S, 28 * S, 28 * S), radius=7 * S, fill=ACCENT)
    line(d, [(12, 22), (12, 10), (21, 10)], 2.6, ON_ACCENT)
    line(d, [(12, 16), (19, 16)], 2.6, ON_ACCENT)
    for y in (11, 16, 21):
        line(d, [(2, y), (4, y)], 1.6, ACCENT)
        line(d, [(28, y), (30, y)], 1.6, ACCENT)
    return img


SVG = """<svg xmlns="http://www.w3.org/2000/svg" width="128" height="128" viewBox="0 0 32 32">
  <rect x="4" y="4" width="24" height="24" rx="7" fill="#3dd6a3"/>
  <path d="M12 22V10h9M12 16h7" stroke="#06281c" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round" fill="none"/>
  <path d="M2 11h2M2 16h2M2 21h2M28 11h2M28 16h2M28 21h2" stroke="#3dd6a3" stroke-width="1.6" stroke-linecap="round"/>
</svg>
"""

if __name__ == "__main__":
    big = draw()
    big.resize((512, 512), Image.LANCZOS).save(HERE / "icon.png")
    big.resize((256, 256), Image.LANCZOS).save(HERE / "icon.ico",
                                               sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    (HERE / "logo.svg").write_text(SVG, "utf-8")
    print("ok:", [p.name for p in HERE.iterdir()])
