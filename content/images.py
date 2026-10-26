"""Dotline-style image templates for Reddit posts: black background, dot grid, one red accent,
IBM Plex Mono. Built from the real widget screenshots in assets/dotline.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).parent
ASSETS = HERE / "assets" / "dotline"
FONTS = HERE / "fonts"

BG = (0, 0, 0)
GRID = (34, 34, 34)
WHITE = (240, 240, 240)
GREY = (150, 150, 150)
RED = (215, 25, 32)


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONTS / ("IBMPlexMono-SemiBold.ttf" if bold else "IBMPlexMono-Regular.ttf")), size)


def canvas(w: int, h: int, step: int = 44) -> Image.Image:
    img = Image.new("RGB", (w, h), BG)
    d = ImageDraw.Draw(img)
    for y in range(step // 2, h, step):
        for x in range(step // 2, w, step):
            d.ellipse((x - 2, y - 2, x + 2, y + 2), fill=GRID)
    return img


def logo(height: int) -> Image.Image:
    """The dot-matrix DOTLINE wordmark cropped from the hero image."""
    mark = Image.open(ASSETS / "hero.png").convert("RGB").crop((92, 245, 756, 365))
    return mark.resize((int(mark.width * height / mark.height), height), Image.LANCZOS)


def asset(name: str) -> Image.Image:
    p = Path(name)
    return Image.open(p if p.exists() else ASSETS / name).convert("RGBA")


def wrap(text: str, f: ImageFont.FreeTypeFont, width: int) -> list[str]:
    lines = []
    for para in text.split("\n"):
        line = ""
        for word in para.split():
            test = f"{line} {word}".strip()
            if f.getlength(test) <= width or not line:
                line = test
            else:
                lines.append(line)
                line = word
        lines.append(line)
    return lines


def header(img: Image.Image, title: str, subtitle: str = "", pad: int = 72) -> int:
    """Draw title/subtitle at the top; return y where content can start."""
    d = ImageDraw.Draw(img)
    y = pad
    tf = font(64, bold=True)
    for line in wrap(title, tf, img.width - 2 * pad - 60):  # keep clear of the red dot
        d.text((pad, y), line, font=tf, fill=WHITE)
        y += 80
    if subtitle:
        sf = font(34)
        y += 8
        for line in wrap(subtitle, sf, img.width - 2 * pad):
            d.text((pad, y), line, font=sf, fill=GREY)
            y += 48
    d.ellipse((img.width - pad - 18, pad + 22, img.width - pad, pad + 40), fill=RED)  # the one red dot
    return y + 40


def showcase(rows: list[list[str]], out: str, title: str = "", subtitle: str = "",
             size: tuple[int, int] = (1440, 1080), gap: int = 28, with_logo: bool = True) -> str:
    """Grid of widget screenshots, each row scaled to the full width."""
    img = canvas(*size)
    pad = 72
    top = header(img, title, subtitle) if title else pad
    bottom = size[1] - pad - (70 if with_logo else 0)
    scaled_rows = []
    for row in rows:
        ims = [asset(n) for n in row]
        h = 1000  # common height, then scale whole row to width
        ims = [im.resize((int(im.width * h / im.height), h), Image.LANCZOS) for im in ims]
        total = sum(im.width for im in ims) + gap * (len(ims) - 1)
        k = (size[0] - 2 * pad) / total
        scaled_rows.append([im.resize((int(im.width * k), int(im.height * k)), Image.LANCZOS) for im in ims])
    total_h = sum(r[0].height for r in scaled_rows) + gap * (len(scaled_rows) - 1)
    if total_h > bottom - top:  # shrink everything to fit vertically
        k = (bottom - top) / total_h
        scaled_rows = [[im.resize((int(im.width * k), int(im.height * k)), Image.LANCZOS) for im in r]
                       for r in scaled_rows]
        total_h = bottom - top
    y = top + (bottom - top - total_h) // 2
    for r in scaled_rows:
        x = (size[0] - (sum(im.width for im in r) + gap * (len(r) - 1))) // 2
        for im in r:
            img.paste(im, (x, y), im)
            x += im.width + gap
        y += r[0].height + gap
    if with_logo:
        mark = logo(44)
        img.paste(mark, (pad, size[1] - pad - mark.height))
    img.save(out, optimize=True)
    return out


def text_card(title: str, items: list[str], out: str, footer: str = "",
              size: tuple[int, int] = (1440, 1080)) -> str:
    """Title plus a list with dot bullets (first bullet red)."""
    img = canvas(*size)
    d = ImageDraw.Draw(img)
    pad = 72
    y = header(img, title) + 10
    f = font(38)
    for i, item in enumerate(items):
        d.ellipse((pad, y + 16, pad + 16, y + 32), fill=RED if i == 0 else WHITE)
        for line in wrap(item, f, size[0] - 2 * pad - 50):
            d.text((pad + 44, y), line, font=f, fill=WHITE)
            y += 54
        y += 22
    mark = logo(44)
    img.paste(mark, (pad, size[1] - pad - mark.height))
    if footer:
        ff = font(30)
        d.text((size[0] - pad - ff.getlength(footer), size[1] - pad - 36), footer, font=ff, fill=GREY)
    img.save(out, optimize=True)
    return out
