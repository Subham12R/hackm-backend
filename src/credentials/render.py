"""Draw a personalised certificate onto a certificate type's artwork.

A layout is a list of placed fields. Positions and sizes are fractions of the artwork, so the admin
editor can place them on a scaled preview and they land in the same spot on the full-size image:

    {"field": "recipient_name", "x": 0.5, "y": 0.42, "size": 0.06, "font": "script", "color": "#1c0704",
     "align": "center", "max_width": 0.7}

`x` is the left edge, centre or right edge of the text (by `align`); `y` is the text's vertical middle;
`size` is the font size as a fraction of the image height. A "qr" field is a square whose top-left
corner is at x/y and whose side is `size` of the image height. A "text" field prints its "text".
"""

import io
from dataclasses import dataclass
from datetime import date
from functools import cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

FONT_DIR = Path(__file__).parent / "fonts"
FONTS = {
    "sans": "Poppins-Regular.ttf",
    "sans-semibold": "Poppins-SemiBold.ttf",
    "sans-bold": "Poppins-Bold.ttf",
    "serif": "PlayfairDisplay.ttf",
    "script": "GreatVibes-Regular.ttf",
    "mono": "SpaceMono-Regular.ttf",
}
FIELDS = ("recipient_name", "team_name", "event_name", "stage_name", "placement", "track", "issue_date", "credential_id", "text", "qr")
ALIGN_ANCHORS = {"left": "lm", "center": "mm", "right": "rm"}
# Certificates are printed at A4 300 dpi at most; bigger artwork is scaled down before drawing.
MAX_SIDE = 3508


@dataclass(frozen=True)
class Values:
    recipient_name: str
    team_name: str
    event_name: str
    stage_name: str
    placement: str
    track: str
    issue_date: date
    credential_id: str
    verify_url: str


@dataclass(frozen=True)
class Rendered:
    png: bytes
    pdf: bytes
    width: int
    height: int


@cache
def _font_bytes(name: str) -> bytes:
    return (FONT_DIR / FONTS.get(name, FONTS["sans"])).read_bytes()


def _font(name: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(io.BytesIO(_font_bytes(name)), max(size, 4))


def _color(value: object) -> str:
    text = str(value or "#000000")
    return text if len(text) in (4, 7) and text.startswith("#") else "#000000"


def _text_for(field: dict, values: Values) -> str:
    kind = field.get("field")
    if kind == "text":
        return str(field.get("text", ""))
    if kind == "issue_date":
        return values.issue_date.strftime("%d %B %Y").lstrip("0")
    if kind == "credential_id":
        return f"{field.get('prefix', 'Credential ID: ')}{values.credential_id}"
    return str(getattr(values, kind, "") or "")


def open_artwork(data: bytes) -> Image.Image:
    image = Image.open(io.BytesIO(data))
    image.load()
    image = image.convert("RGB")
    if max(image.size) > MAX_SIDE:
        image.thumbnail((MAX_SIDE, MAX_SIDE), Image.LANCZOS)
    return image


def draw(artwork: Image.Image, layout: list[dict], values: Values) -> Image.Image:
    image = artwork.copy()
    width, height = image.size
    canvas = ImageDraw.Draw(image)
    for field in layout:
        kind = field.get("field")
        if kind not in FIELDS:
            continue
        x = float(field.get("x", 0.5)) * width
        y = float(field.get("y", 0.5)) * height
        if kind == "qr":
            _draw_qr(image, values.verify_url, int(x), int(y), int(float(field.get("size", 0.12)) * height), _color(field.get("color")))
            continue
        text = _text_for(field, values)
        if not text:
            continue
        if field.get("uppercase"):
            text = text.upper()
        size = int(float(field.get("size", 0.04)) * height)
        font_name = str(field.get("font", "sans"))
        font = _font(font_name, size)
        max_width = float(field.get("max_width", 0) or 0) * width
        # Long names shrink until they fit the box instead of running off the artwork.
        while max_width and size > 8 and canvas.textlength(text, font=font) > max_width:
            size = int(size * 0.92)
            font = _font(font_name, size)
        anchor = ALIGN_ANCHORS.get(str(field.get("align", "center")), "mm")
        canvas.text((x, y), text, font=font, fill=_color(field.get("color")), anchor=anchor)
    return image


def _draw_qr(image: Image.Image, url: str, left: int, top: int, side: int, color: str) -> None:
    import segno

    if side < 16:
        return
    buffer = io.BytesIO()
    segno.make(url, error="m").save(buffer, kind="png", scale=10, border=1, dark=color, light="#ffffff")
    code = Image.open(io.BytesIO(buffer.getvalue())).convert("RGB").resize((side, side), Image.NEAREST)
    image.paste(code, (left, top))


def render(artwork: Image.Image, layout: list[dict], values: Values) -> Rendered:
    image = draw(artwork, layout, values)
    png = io.BytesIO()
    image.save(png, format="PNG", optimize=True)
    pdf = io.BytesIO()
    image.save(pdf, format="PDF", resolution=300.0, title=f"{values.event_name} certificate {values.credential_id}")
    return Rendered(png=png.getvalue(), pdf=pdf.getvalue(), width=image.width, height=image.height)


def preview_jpeg(artwork: Image.Image, layout: list[dict], values: Values, max_width: int = 1400) -> bytes:
    image = draw(artwork, layout, values)
    if image.width > max_width:
        image.thumbnail((max_width, max_width), Image.LANCZOS)
    out = io.BytesIO()
    image.save(out, format="JPEG", quality=85)
    return out.getvalue()
