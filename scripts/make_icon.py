"""Generate assets/app.ico (16–256 px) and assets/app.png from code with Pillow.

Run:  python scripts/make_icon.py
The generated files are committed, so this only needs re-running after a design change.
"""

from __future__ import annotations

import os

from PIL import Image, ImageDraw, ImageFilter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS = os.path.join(ROOT, "assets")
S = 1024  # master canvas


def lerp(a, b, t):
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def gradient(size, top, bottom, diagonal=True):
    img = Image.new("RGBA", (size, size))
    px = img.load()
    for y in range(size):
        for x in range(size):
            t = ((x + y) / (2 * size)) if diagonal else y / size
            px[x, y] = lerp(top, bottom, t) + (255,)
    return img


def master() -> Image.Image:
    small = 256  # build gradients small, then scale (fast and smooth)
    base = Image.new("RGBA", (S, S), (0, 0, 0, 0))

    # Rounded-square tile with a deep blue gradient.
    tile = gradient(small, (40, 56, 110), (14, 20, 40)).resize((S, S), Image.BICUBIC)
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle((40, 40, S - 40, S - 40), radius=210, fill=255)
    base.paste(tile, (0, 0), mask)

    # Subtle inner highlight.
    hl = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(hl).rounded_rectangle((44, 44, S - 44, S - 44), radius=206, outline=(255, 255, 255, 40), width=6)
    base.alpha_composite(hl)

    # Accent gradient (teal -> blue) used for the monitor and the nexus.
    accent = gradient(small, (45, 212, 191), (79, 140, 255)).resize((S, S), Image.BICUBIC)

    shape = Image.new("L", (S, S), 0)
    d = ImageDraw.Draw(shape)
    # Monitor frame.
    d.rounded_rectangle((190, 230, 834, 690), radius=70, outline=255, width=64)
    # Stand.
    d.rounded_rectangle((470, 690, 554, 790), radius=12, fill=255)
    d.rounded_rectangle((350, 770, 674, 830), radius=30, fill=255)
    # Nexus: three linked nodes inside the screen.
    nodes = [(370, 545), (512, 380), (654, 545)]
    for (x1, y1), (x2, y2) in zip(nodes, nodes[1:] + nodes[:1]):
        d.line((x1, y1, x2, y2), fill=255, width=34)
    for x, y in nodes:
        d.ellipse((x - 62, y - 62, x + 62, y + 62), fill=255)

    glow = Image.new("RGBA", (S, S), (45, 212, 191, 0))
    glow.putalpha(shape.filter(ImageFilter.GaussianBlur(28)).point(lambda v: v * 0.45))
    base.alpha_composite(glow)
    fg = accent.copy()
    fg.putalpha(shape)
    base.alpha_composite(fg)

    # Node centres punched with the tile colour for a crisp "ring" look.
    rings = Image.new("L", (S, S), 0)
    rd = ImageDraw.Draw(rings)
    for x, y in nodes:
        rd.ellipse((x - 26, y - 26, x + 26, y + 26), fill=255)
    centre = Image.new("RGBA", (S, S), (16, 24, 48, 255))
    centre.putalpha(rings)
    base.alpha_composite(centre)
    return base


def main() -> None:
    os.makedirs(ASSETS, exist_ok=True)
    img = master()
    img.resize((512, 512), Image.LANCZOS).save(os.path.join(ASSETS, "app.png"))
    sizes = [(16, 16), (20, 20), (24, 24), (32, 32), (40, 40), (48, 48), (64, 64), (128, 128), (256, 256)]
    img.resize((256, 256), Image.LANCZOS).save(os.path.join(ASSETS, "app.ico"), sizes=sizes)
    print("Wrote assets/app.ico and assets/app.png")


if __name__ == "__main__":
    main()
