#!/usr/bin/python3
"""Build the Odile website icon from the painted head.

Dev-side only: Pillow is a build dependency, not a runtime one. The public
server serves the committed PNG/ICO files; it never runs this script.

    /usr/bin/python3 tools/icon_build.py           # write the set
    /usr/bin/python3 tools/icon_build.py --check   # compare, write nothing

Provenance
----------
Artwork: Lucas Cranach the Elder, *Saints Christina and Ottilia*, 1506,
reverse of the right wing of the Katharinenaltar, Dresden (Galerie Alte
Meister). Public domain. The head on the right is Saint Ottilia.

Reproduction: Wikimedia Commons, "Lucas Cranach the Elder - Saints Christina
and Ottilia - Google Art Project.jpg", 2210x4231, sha256
d8ef03238bacabd1c7b244bad98e85f5053a980e29eab4741a10002bf0b4f421.

`web/icon-source.jpg` is the 475x475 detail (1436,521)-(1911,996) of that
reproduction, resampled to 475x475; `web/icon-source.txt` records the URL and
both hashes. `web/icon-source-supplied.jpg` is the 480x480 JPEG Savio supplied
on 2026-11-13, byte-identical, kept as the record of the instruction.

Derivation
----------
The icon is one rendition of the painting, not a second drawing. The fixed
square crop is warmed to the sheet's grade, rendered once at 512 px, and
downsampled with LANCZOS to every size (16, 32, 48, 180, 192, 512). No size
has its own pass; the 16 px tab entry is the 512 px master downsampled, and
`tests/test_web_icon.py` asserts exactly that.

Why a warm grade and not a four-ink cut: at 16 px a posterised head loses the
skin hue, and the pale face over a dark ground reads as bone - a skull - to
every viewer asked. The warm grade keeps the painting's own cue: skin against
a neutral wimple. The grade is measured on the master and recorded in
BRAND.md §Name and mark and §Palette. Nothing is sharpened and nothing is
"restored": the craquelure stays at 512 and dissolves at 16.

The crop is square and fixed: 420 px at (28, 40) in the 475 px detail.
`derive()` is deterministic, so the committed bytes are reproducible.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFont

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
SOURCE = WEB / "icon-source.jpg"
PROOF = ROOT / "docs" / "brand" / "ICON-PROOF.png"

# The square crop inside the 475x475 detail: face, veil and the halo arc.
CROP = (28, 40, 448, 460)

# The measured warm grade. Saturation is pulled down from the source's 0.20
# relative to the Ruysch's 0.41; the channel curves warm the highlights and
# keep the blacks warm without inventing a colour. Measured on the master:
# mean HSV saturation 0.34, mean hue 42 degrees, R-B 12 / 29 / 56 across the
# luminance deciles (deepest, 10-25 %, above the 25th percentile).
GRADE_SATURATION = 0.92
GRADE_CHANNELS = ((1.05, 4), (1.00, 2), (0.88, 0))

# Sheet and mount colours, from BRAND.md. The icon itself is the painting's
# own colour; these are the paper mount and the proof sheet's ink.
INK = (28, 24, 19)        # --ink #1C1813
SOFT = (74, 65, 54)       # --soft #4A4136
PAPER = (234, 223, 196)   # --paper #EADFC4

# Every file the icon set ships, with its size.
PNG_SIZES = {"icon-32.png": 32, "icon-192.png": 192, "icon-512.png": 512}
ICO_SIZES = (16, 32, 48)
TOUCH_SIZE = 180
TOUCH_MATTE = 12  # paper on every side at 180: a plate on paper, opaque

# The proof sheet's geometry. The tab is 8 x 48 px tall so the largest
# enlargement is shown whole; every 8x sits on the tab, beside its own 1x.
PROOF_SCALE = 8
PROOF_MARGIN = 40
PROOF_PAD = 24       # between the tab's edge and the first icon
PROOF_INNER = 20     # a 1x icon to its own 8x
PROOF_GAP = 32       # cell to cell
PROOF_CHROME = 22    # the browser chrome above the tab
PROOF_LABEL = 26     # the label band above the icons


def derive(source: Image.Image) -> Image.Image:
    """Return the 512 px master: the fixed crop, warm-graded. Deterministic."""
    crop = source.convert("RGB").crop(CROP).resize((512, 512), Image.Resampling.LANCZOS)
    image = ImageEnhance.Color(crop).enhance(GRADE_SATURATION)
    channels = []
    for band, (gain, lift) in zip(image.split(), GRADE_CHANNELS):
        table = [min(255, round(value * gain + lift)) for value in range(256)]
        channels.append(band.point(table))
    return Image.merge("RGB", channels)


def _sized(master: Image.Image, size: int) -> Image.Image:
    """The one downsample every size goes through."""
    return master.resize((size, size), Image.Resampling.LANCZOS)


def build() -> dict[str, bytes]:
    """Return every output file's bytes, keyed by its path under the repo."""
    source = Image.open(SOURCE)
    master = derive(source)
    files: dict[str, bytes] = {}

    for name, size in PNG_SIZES.items():
        buf = io.BytesIO()
        _sized(master, size).save(buf, "PNG", optimize=True)
        files[f"web/{name}"] = buf.getvalue()

    # Every tab size is the same master downsampled: 16, 32 and 48 are one
    # rendition, handed to Pillow at their exact size so it uses them as is.
    frames = [_sized(master, size) for size in ICO_SIZES]
    buf = io.BytesIO()
    _sized(master, 512).save(
        buf,
        "ICO",
        sizes=[(size, size) for size in ICO_SIZES],
        append_images=frames,
        bitmap_format="bmp",
    )
    files["web/favicon.ico"] = buf.getvalue()

    # iOS masks the icon and paints transparency black: the touch icon is an
    # opaque paper mount, the head inset by a 12 px matte.
    inner = TOUCH_SIZE - 2 * TOUCH_MATTE
    touch = Image.new("RGB", (TOUCH_SIZE, TOUCH_SIZE), PAPER)
    touch.paste(_sized(master, inner), (TOUCH_MATTE, TOUCH_MATTE))
    buf = io.BytesIO()
    touch.save(buf, "PNG", optimize=True)
    files["web/apple-touch-icon.png"] = buf.getvalue()

    files["docs/brand/ICON-PROOF.png"] = _proof(master)
    return files


def _font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    names = (
        "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    )
    for name in names:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, width: int) -> list[str]:
    """Break a line of proof-sheet captions to the width it may use."""
    lines, line = [], ""
    for word in text.split():
        trial = f"{line} {word}".strip()
        if line and draw.textlength(trial, font=font) > width:
            lines.append(line)
            line = word
        else:
            line = trial
    if line:
        lines.append(line)
    return lines


def _cell_left(size: int) -> int:
    """The left edge of one size's cell inside a proof strip."""
    x = PROOF_PAD
    for earlier in ICO_SIZES:
        if earlier == size:
            return x
        x += (PROOF_SCALE + 1) * earlier + PROOF_INNER + PROOF_GAP
    raise ValueError(size)


def _strip(frames: dict[int, Image.Image], labels: dict[int, str], width: int, *,
           light: bool) -> Image.Image:
    """One tab strip: every tab size at 1x on the tab, its 8x beside it.

    The tab is `PROOF_SCALE * 48` px tall, so the 48 px enlargement is whole;
    each cell is a label, the 1x icon on the tab and, to its right, the same
    pixels at 8x nearest-neighbour. Nothing is pasted on the chrome.
    """
    top = PROOF_CHROME + PROOF_LABEL
    height = top + PROOF_SCALE * max(ICO_SIZES) + PROOF_PAD
    strip = Image.new("RGB", (width, height), (222, 225, 230) if light else (32, 33, 36))
    draw = ImageDraw.Draw(strip)
    tab = (241, 243, 244) if light else (41, 42, 45)
    draw.rectangle((0, PROOF_CHROME, width - 1, height - 1), fill=tab)
    ink = (28, 24, 19) if light else (234, 223, 196)
    hair = (196, 199, 204) if light else (78, 80, 84)

    for size in ICO_SIZES:
        icon = frames[size]
        x = _cell_left(size)
        draw.text((x, PROOF_CHROME + 6), labels[size], fill=ink, font=_font(14))
        strip.paste(icon, (x, top))
        # A hairline one pixel outside the canvas marks the 16/32/48 box.
        draw.rectangle((x - 1, top - 1, x + size, top + size), outline=hair)
        big = icon.resize((size * PROOF_SCALE, size * PROOF_SCALE),
                          Image.Resampling.NEAREST)
        strip.paste(big, (x + size + PROOF_INNER, top))
    return strip


def _proof(master: Image.Image) -> bytes:
    """The at-size sheet: 16, 32 and 48 px at 1x and 8x, light and dark."""
    width, margin = 1120, PROOF_MARGIN
    inner = width - 2 * margin
    frames = {size: _sized(master, size) for size in ICO_SIZES}
    labels = {size: "%d px · 1× and 8×" % size for size in ICO_SIZES}
    strips = [_strip(frames, labels, inner, light=light) for light in (True, False)]

    heading, body = _font(17), _font(15)
    probe = ImageDraw.Draw(Image.new("RGB", (width, 8)))
    intro = [line for text in (
        "Lucas Cranach the Elder, Saints Christina and Ottilia "
        "(Katharinenaltar, Dresden), 1506.",
        "One rendition: the 512 px warm-graded crop, downsampled with LANCZOS "
        "to every size. 8× is nearest-neighbour: every square is one of the "
        "icon's own pixels.",
    ) for line in _wrap(probe, text, body, inner)]

    # Generous canvas, cropped to the last row drawn: no fixed height to clip.
    sheet = Image.new("RGB", (width, 4000), PAPER)
    draw = ImageDraw.Draw(sheet)
    draw.text((margin, 24), "Odile website icon — proof at size", fill=INK, font=_font(30))
    y = 66
    for line in intro:
        draw.text((margin, y), line, fill=SOFT, font=body)
        y += 21

    y += 14
    for strip, ground in zip(strips, ("On a light tab strip", "On a dark tab strip")):
        draw.text((margin, y), ground, fill=INK, font=heading)
        y += 26
        sheet.paste(strip, (margin, y))
        y += strip.height + 32

    draw.text((margin, y), "The large sizes at 1×, on paper", fill=INK, font=heading)
    y += 30
    row, line_rows = [], 0
    for size in (TOUCH_SIZE, 192, 512):
        if size == TOUCH_SIZE:
            inner_size = TOUCH_SIZE - 2 * TOUCH_MATTE
            icon = Image.new("RGB", (TOUCH_SIZE, TOUCH_SIZE), PAPER)
            icon.paste(_sized(master, inner_size), (TOUCH_MATTE, TOUCH_MATTE))
            caption = "180 apple-touch · opaque paper mount"
        else:
            icon = _sized(master, size)
            caption = str(size)
        row.append((icon, _wrap(probe, caption, body, size)))
        line_rows = max(line_rows, len(row[-1][1]))
    x = margin
    for icon, caption in row:
        sheet.paste(icon, (x, y))
        for offset, line in enumerate(caption):
            draw.text((x, y + icon.height + 8 + offset * 20), line, fill=SOFT, font=body)
        x += icon.width + 40
    y += 512 + 8 + line_rows * 20

    buf = io.BytesIO()
    sheet.crop((0, 0, width, y + 24)).save(buf, "PNG", optimize=True)
    return buf.getvalue()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="compare the committed bytes with a fresh build; write nothing")
    args = parser.parse_args(argv)

    if not SOURCE.is_file():
        print(f"icon build: missing {SOURCE}", file=sys.stderr)
        return 2

    files = build()
    failures = 0
    for name, data in files.items():
        path = ROOT / name
        if args.check:
            expected = path.read_bytes() if path.is_file() else b""
            status = "ok" if expected == data else "DIFFERS"
            if expected != data:
                failures += 1
            print(f"{status:8s} {name}  {len(data)} bytes  sha256 {hashlib.sha256(data).hexdigest()}")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            print(f"wrote    {name}  {len(data)} bytes  sha256 {hashlib.sha256(data).hexdigest()}")
    if args.check and failures:
        print(f"icon build: {failures} file(s) differ from the committed set", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
