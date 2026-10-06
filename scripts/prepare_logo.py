"""Prepare the supplied logo artwork for use in the app.

    python scripts/prepare_logo.py

`frontend/public/brand/logo-mark.png` arrives as a 500x500 RGB PNG containing the
**full lockup**: the circular emblem on the left and the "Corman Leigh" wordmark
beside it, navy on a white background, with roughly 21% empty margin all round.

Three problems follow:

  * a white background renders as a white block on the dark sidebar
  * the padding makes the mark look small inside whatever box it is given
  * the app draws the wordmark as live text, so baked-in text would appear twice

This script lifts the white to transparency, finds the emblem by looking for the
first wide vertical gap after the dense left-hand cluster, and writes two files:

    logo-mark-web.png   the emblem alone, square, transparent  <- used by the app
    logo-full-web.png   the whole lockup, transparent          <- spare

The original is never modified, so it can be reprocessed if the source changes.
"""

import sys
from pathlib import Path

from PIL import Image

BRAND_DIR = Path(__file__).resolve().parents[2] / "frontend" / "public" / "brand"
SOURCE = BRAND_DIR / "logo-mark.png"
MARK_OUT = BRAND_DIR / "logo-mark-web.png"
FULL_OUT = BRAND_DIR / "logo-full-web.png"

WHITE_CUTOFF = 238          # at least this bright on every channel = background
FEATHER = 26                # levels below the cutoff that fade out, not cut
MIN_GAP = 12                # blank columns that count as a separator
MARGIN = 0.06               # breathing room around the emblem
SIZE = 512


def to_transparent(img: Image.Image) -> Image.Image:
    """Lift the white background, feathering the anti-aliased edge."""
    img = img.convert("RGBA")
    px = img.load()
    w, h = img.size
    for y in range(h):
        for x in range(w):
            r, g, b, a = px[x, y]
            lightest = min(r, g, b)
            if lightest >= WHITE_CUTOFF:
                px[x, y] = (r, g, b, 0)
            elif lightest > WHITE_CUTOFF - FEATHER:
                fade = int(255 * (WHITE_CUTOFF - lightest) / FEATHER)
                px[x, y] = (r, g, b, min(a, fade))
    return img


def square_out(img: Image.Image, path: Path, size: int = SIZE) -> None:
    """Centre the artwork on a transparent square canvas and save it."""
    side = max(img.size)
    canvas_side = int(side * (1 + MARGIN * 2))
    canvas = Image.new("RGBA", (canvas_side, canvas_side), (0, 0, 0, 0))
    canvas.paste(
        img,
        ((canvas_side - img.size[0]) // 2, (canvas_side - img.size[1]) // 2),
        img,
    )
    canvas.resize((size, size), Image.LANCZOS).save(path, format="PNG", optimize=True)


def find_emblem_edge(img: Image.Image) -> int | None:
    """Right-hand edge of the emblem: the first wide blank run after real ink.

    The wordmark's letters leave regular narrow gaps; the space between emblem
    and wordmark is wider than any of them, so the first run of >= MIN_GAP blank
    columns marks the split.
    """
    px = img.load()
    w, h = img.size
    cols = [sum(1 for y in range(h) if px[x, y][3] > 24) for x in range(w)]

    seen_ink = False
    run = 0
    for x, v in enumerate(cols):
        if v > 1:
            if run >= MIN_GAP and seen_ink:
                return x - run
            seen_ink = True
            run = 0
        else:
            run += 1
    return None


def main() -> int:
    if not SOURCE.exists():
        print(f"No artwork at {SOURCE}", file=sys.stderr)
        return 1

    original = Image.open(SOURCE)
    print(f"source          {original.size[0]}x{original.size[1]} {original.mode}")

    img = to_transparent(original)
    bbox = img.getbbox()
    if not bbox:
        print("Artwork is entirely transparent after the white cut.", file=sys.stderr)
        return 1

    lockup = img.crop(bbox)
    print(f"trimmed lockup  {lockup.size[0]}x{lockup.size[1]}")

    edge = find_emblem_edge(lockup)
    if edge is None:
        # No separation found: the file is probably the emblem on its own.
        emblem = lockup
        print("emblem          whole image (no wordmark detected)")
    else:
        emblem = lockup.crop((0, 0, edge, lockup.size[1]))
        emblem = emblem.crop(emblem.getbbox())
        print(f"emblem          {emblem.size[0]}x{emblem.size[1]}  (split at x={edge})")
        ratio = emblem.size[0] / emblem.size[1]
        verdict = "square-ish, looks right" if 0.8 < ratio < 1.25 else "CHECK THIS"
        print(f"                aspect {ratio:.2f} - {verdict}")

    square_out(emblem, MARK_OUT)
    print(f"wrote           {MARK_OUT.name}  {SIZE}x{SIZE} RGBA  "
          f"{MARK_OUT.stat().st_size / 1024:.0f} KB")

    square_out(lockup, FULL_OUT)
    print(f"wrote           {FULL_OUT.name}  {SIZE}x{SIZE} RGBA  "
          f"{FULL_OUT.stat().st_size / 1024:.0f} KB")

    print("\nThe app uses logo-mark-web.png. Re-run after replacing logo-mark.png.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
