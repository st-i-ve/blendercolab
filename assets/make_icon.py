"""Generate the BlendFleet app icon.

Deliberately NOT a copy of the Blender logo, which is a trademark of the
Blender Foundation. This is an original mark that borrows the family
resemblance -- the orbital ring around a body, and Blender's warm orange
against a cool dark ground -- and adds the "render factor" the icon is for:
a properly shaded sphere with a specular hot spot, a soft cast shadow, and
a transparency checker peeking out of one corner the way a render viewport
shows alpha.

Run:  .venv/Scripts/python.exe assets/make_icon.py
"""
from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

OUT_DIR = Path(__file__).parent
SIZE = 1024          # master render; downsampled copies are written too
SS = 2               # supersampling factor for smooth edges

# Blender-adjacent palette, but our own combination
BG_TOP = (32, 36, 44)
BG_BOT = (18, 20, 26)
ORANGE = (245, 121, 42)
ORANGE_HI = (255, 178, 110)
ORANGE_DK = (168, 68, 12)
BLUE = (38, 87, 135)
RING = (240, 240, 245)


def _lerp(a, b, t):
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def _rounded_rect_mask(size: int, radius: int) -> Image.Image:
    m = Image.new("L", (size, size), 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, size - 1, size - 1],
                                        radius=radius, fill=255)
    return m


def _background(size: int) -> Image.Image:
    """Vertical gradient ground."""
    img = Image.new("RGB", (size, size))
    d = ImageDraw.Draw(img)
    for y in range(size):
        d.line([(0, y), (size, y)], fill=_lerp(BG_TOP, BG_BOT, y / size))
    return img


def _checker(size: int, cell: int) -> Image.Image:
    """Transparency checker -- the visual shorthand for 'this is a render'."""
    img = Image.new("RGB", (size, size), (58, 62, 70))
    d = ImageDraw.Draw(img)
    for y in range(0, size, cell):
        for x in range(0, size, cell):
            if (x // cell + y // cell) % 2 == 0:
                d.rectangle([x, y, x + cell - 1, y + cell - 1], fill=(44, 48, 56))
    return img


def _sphere(diameter: int) -> Image.Image:
    """A shaded sphere: lambert falloff, specular hot spot, rim light."""
    img = Image.new("RGBA", (diameter, diameter), (0, 0, 0, 0))
    px = img.load()
    r = diameter / 2
    # light direction (normalised), pointing up-left toward the viewer
    lx, ly, lz = -0.45, -0.62, 0.64
    for y in range(diameter):
        for x in range(diameter):
            nx = (x - r + 0.5) / r
            ny = (y - r + 0.5) / r
            d2 = nx * nx + ny * ny
            if d2 > 1.0:
                continue
            nz = math.sqrt(1.0 - d2)

            lam = max(0.0, nx * lx + ny * ly + nz * lz)      # diffuse
            base = _lerp(ORANGE_DK, ORANGE, min(1.0, lam * 1.15 + 0.08))

            # specular: Blinn-Phong-ish, tight and bright
            hx, hy, hz = lx, ly, lz + 1.0
            hn = math.sqrt(hx * hx + hy * hy + hz * hz)
            spec = max(0.0, (nx * hx + ny * hy + nz * hz) / hn) ** 48
            base = _lerp(base, ORANGE_HI, min(1.0, spec * 1.6))

            # rim light from the cool background, strongest at the silhouette
            rim = max(0.0, (1.0 - nz)) ** 3
            base = _lerp(base, BLUE, min(0.55, rim * 0.75))

            # antialias the silhouette
            edge = min(1.0, (1.0 - math.sqrt(d2)) * r * 0.9)
            px[x, y] = (*base, int(255 * max(0.0, edge)))
    return img


def build(size: int = SIZE) -> Image.Image:
    S = size * SS
    img = _background(S).convert("RGBA")

    # --- transparency checker in the lower-right, clipped to a wedge -------
    checker = _checker(S, S // 16).convert("RGBA")
    wedge = Image.new("L", (S, S), 0)
    ImageDraw.Draw(wedge).polygon(
        [(S, int(S * 0.46)), (S, S), (int(S * 0.46), S)], fill=70)
    img.paste(checker, (0, 0), wedge)

    d = ImageDraw.Draw(img)
    cx = cy = S // 2

    # --- orbital ring: a tilted ellipse, drawn behind the sphere ----------
    ring_w = int(S * 0.040)
    rx, ry = int(S * 0.355), int(S * 0.150)
    ring_layer = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(ring_layer).ellipse(
        [cx - rx, cy - ry, cx + rx, cy + ry], outline=(*RING, 255), width=ring_w)
    ring_layer = ring_layer.rotate(-26, resample=Image.BICUBIC, center=(cx, cy))
    img.alpha_composite(ring_layer)

    # --- cast shadow ------------------------------------------------------
    sph_d = int(S * 0.46)
    shadow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).ellipse(
        [cx - sph_d // 2, cy - sph_d // 2 + int(S * 0.055),
         cx + sph_d // 2, cy + sph_d // 2 + int(S * 0.055)],
        fill=(0, 0, 0, 150))
    img.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(S // 45)))

    # --- the sphere -------------------------------------------------------
    sph = _sphere(sph_d)
    img.alpha_composite(sph, (cx - sph_d // 2, cy - sph_d // 2))

    # --- front half of the ring, over the sphere --------------------------
    front = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(front).ellipse(
        [cx - rx, cy - ry, cx + rx, cy + ry], outline=(*RING, 255), width=ring_w)
    keep = Image.new("L", (S, S), 0)
    ImageDraw.Draw(keep).rectangle([0, cy, S, S], fill=255)
    front.putalpha(Image.composite(front.getchannel("A"),
                                   Image.new("L", (S, S), 0), keep))
    front = front.rotate(-26, resample=Image.BICUBIC, center=(cx, cy))
    img.alpha_composite(front)

    # --- scanline sweep: the "render in progress" cue ---------------------
    scan = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    sd = ImageDraw.Draw(scan)
    for i in range(3):
        y = int(S * (0.30 + i * 0.021))
        sd.line([(int(S * 0.10), y), (int(S * 0.90), y)],
                fill=(255, 255, 255, 26 - i * 7), width=max(1, S // 340))
    img.alpha_composite(scan)

    # --- rounded-square crop + subtle inner stroke ------------------------
    img.putalpha(_rounded_rect_mask(S, int(S * 0.22)))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([2, 2, S - 3, S - 3], radius=int(S * 0.22),
                        outline=(255, 255, 255, 26), width=max(2, S // 300))

    return img.resize((size, size), Image.LANCZOS)


def main() -> None:
    master = build(SIZE)
    master.save(OUT_DIR / "blendfleet_icon.png")
    print(f"wrote {OUT_DIR / 'blendfleet_icon.png'}  {SIZE}x{SIZE}")

    for s in (512, 256, 128, 64, 32):
        master.resize((s, s), Image.LANCZOS).save(OUT_DIR / f"blendfleet_icon_{s}.png")
        print(f"wrote blendfleet_icon_{s}.png")

    # Windows .ico for the packaged exe
    master.save(OUT_DIR / "blendfleet.ico",
                sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])
    print(f"wrote {OUT_DIR / 'blendfleet.ico'}")


if __name__ == "__main__":
    main()
