"""Derive the BlendFleet mark and its variations from assets/newLogo.png.

The source is a contact sheet with three treatments; the monochrome mark
(bottom) is the primary, because it holds up at 16 px and — being a single
silhouette — can be tinted to whatever accent colour the user picks.

Everything here is derived, never hand-traced, so re-running after a source
change regenerates the whole set.

Run:  .venv/Scripts/python.exe assets/make_logo.py
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter

HERE = Path(__file__).parent
SOURCE = HERE / "newLogo.png"
OUT = HERE / "logo"

# Accent palette offered in Settings. The mark is tinted to the active one.
ACCENTS = {
    "orange": (245, 121, 42),
    "green": (61, 191, 122),
    "purple": (155, 122, 232),
    "blue": (74, 150, 240),
    "red": (232, 88, 88),
}

SHELL_BG = (22, 24, 29)      # matches theme.py SHELL background


def _autocrop_mark(sheet: Image.Image) -> Image.Image:
    """Cut the monochrome mark out of the bottom panel of the contact sheet.

    Found by content rather than fixed pixel offsets: take the bottom half,
    locate the non-white bounding box, and trim to it. If the source sheet is
    ever re-exported at a different size this keeps working.
    """
    w, h = sheet.size
    # Start below the black panel in the upper-left quadrant, which ends around
    # 0.55h -- including it would merge the panel edge into the mark's bbox and
    # throw the crop off-centre. Measured against the current sheet: panel ink
    # stops by y=0.55h, the mark runs roughly 0.62h-0.89h.
    top = int(h * 0.58)
    bottom = sheet.convert("L").crop((0, top, w, h))
    # non-white = the mark. invert so the mark is the bright region getbbox wants
    mask = ImageChops.invert(bottom).point(lambda p: 255 if p > 40 else 0)
    box = mask.getbbox()
    if box is None:
        raise SystemExit("no mark found in the bottom half of newLogo.png")
    x0, y0, x1, y1 = box
    return sheet.convert("RGBA").crop((x0, y0 + top, x1, y1 + top))


def _to_alpha_silhouette(mark: Image.Image) -> Image.Image:
    """White mark on transparency: alpha carries the shape, RGB is flat white.

    This is the tintable master — recolouring is then a single fill, so the
    accent setting can restyle the mark at runtime without new art.
    """
    grey = mark.convert("L")
    alpha = ImageChops.invert(grey)          # dark ink -> opaque
    alpha = alpha.point(lambda p: 0 if p < 30 else p)   # kill JPEG-ish haze
    out = Image.new("RGBA", mark.size, (255, 255, 255, 0))
    out.putalpha(alpha)
    white = Image.new("RGBA", mark.size, (255, 255, 255, 255))
    white.putalpha(alpha)
    return white


def _square_pad(img: Image.Image, ratio: float = 0.78) -> Image.Image:
    """Centre the mark on a transparent square, occupying `ratio` of the side.

    Icons need consistent optical weight; without this the mark's own bbox
    would make every exported size a slightly different scale.
    """
    side = int(max(img.size) / ratio)
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    scale = (side * ratio) / max(img.size)
    resized = img.resize((max(1, int(img.width * scale)),
                          max(1, int(img.height * scale))), Image.LANCZOS)
    canvas.paste(resized, ((side - resized.width) // 2,
                           (side - resized.height) // 2), resized)
    return canvas


def _tint(silhouette: Image.Image, rgb: tuple[int, int, int]) -> Image.Image:
    solid = Image.new("RGBA", silhouette.size, (*rgb, 255))
    solid.putalpha(silhouette.getchannel("A"))
    return solid


def _app_icon(silhouette: Image.Image, accent: tuple[int, int, int],
              size: int = 1024) -> Image.Image:
    """Rounded-square app icon: tinted mark on the app's own shell colour."""
    ss = 2
    S = size * ss
    canvas = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(canvas).rounded_rectangle(
        [0, 0, S - 1, S - 1], radius=int(S * 0.22), fill=(*SHELL_BG, 255))

    mark = _tint(silhouette, accent)
    target = int(S * 0.60)
    scale = target / max(mark.size)
    mark = mark.resize((int(mark.width * scale), int(mark.height * scale)),
                       Image.LANCZOS)

    glow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    glow.paste(mark, ((S - mark.width) // 2, (S - mark.height) // 2), mark)
    canvas.alpha_composite(glow.filter(ImageFilter.GaussianBlur(S // 60)))
    canvas.alpha_composite(glow)

    ImageDraw.Draw(canvas).rounded_rectangle(
        [2, 2, S - 3, S - 3], radius=int(S * 0.22),
        outline=(255, 255, 255, 22), width=max(2, S // 300))
    return canvas.resize((size, size), Image.LANCZOS)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    sheet = Image.open(SOURCE)
    mark = _autocrop_mark(sheet)
    silhouette = _square_pad(_to_alpha_silhouette(mark))
    silhouette.save(OUT / "mark-white.png")
    print(f"mark-white.png              {silhouette.size[0]}px  tintable master")

    _tint(silhouette, (17, 19, 24)).save(OUT / "mark-black.png")
    print("mark-black.png              for light backgrounds")

    for name, rgb in ACCENTS.items():
        _tint(silhouette, rgb).resize((512, 512), Image.LANCZOS) \
            .save(OUT / f"mark-{name}.png")
    print(f"mark-{{{','.join(ACCENTS)}}}.png   accent variants")

    icon = _app_icon(silhouette, ACCENTS["orange"])
    icon.save(OUT / "app-icon.png")
    for s in (512, 256, 128, 64, 48, 32, 16):
        icon.resize((s, s), Image.LANCZOS).save(OUT / f"app-icon-{s}.png")
    icon.save(OUT / "blendfleet.ico",
              sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])
    print("app-icon*.png + blendfleet.ico   window + exe")

    # Small monochrome glyph for the title bar / rail, no rounded plate.
    for s in (24, 32, 48):
        _tint(silhouette, (255, 255, 255)).resize((s, s), Image.LANCZOS) \
            .save(OUT / f"glyph-{s}.png")
    print("glyph-{24,32,48}.png        in-app chrome")


if __name__ == "__main__":
    main()
