"""
Generate synthetic business card images for testing.

Produces:
  samples/card_upright.jpg  - a normal 2000x1200 "photo" of a card
  samples/card_rotated.jpg  - the SAME card stored sideways with an EXIF
                              orientation tag, exactly like a phone photo.
                              Opens upright in Preview; decodes sideways.

Run: ./.venv/bin/python tools/make_test_card.py
"""

import pathlib

from PIL import Image, ImageDraw, ImageFont

OUT = pathlib.Path("samples")
OUT.mkdir(exist_ok=True)

W, H = 2000, 1200


def _font(size: int):
    """Use a real system font so the text is sharp enough to be readable."""
    for path in (
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
    ):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


def draw_card() -> Image.Image:
    img = Image.new("RGB", (W, H), "#f7f5f0")
    d = ImageDraw.Draw(img)

    # Accent bar down the left edge
    d.rectangle([0, 0, 90, H], fill="#12395c")
    # Company mark
    d.ellipse([170, 130, 290, 250], fill="#12395c")

    d.text((330, 150), "NIMBUS LOGISTICS", font=_font(78), fill="#12395c")
    d.text((330, 248), "freight forwarding since 1998", font=_font(38), fill="#8a8578")

    d.line([170, 400, W - 170, 400], fill="#d8d3c8", width=3)

    d.text((170, 470), "Asha Rao", font=_font(104), fill="#1a1a1a")
    d.text((176, 610), "Senior Sales Manager", font=_font(52), fill="#5c5750")

    # Small print -- this is the text that blurs away if you downscale too far.
    small = _font(42)
    d.text((176, 760), "+91 98200 12345", font=small, fill="#1a1a1a")
    d.text((176, 830), "A.Rao@NimbusLogistics.IN", font=small, fill="#1a1a1a")
    d.text((176, 900), "Unit 4, Andheri East, Mumbai 400069", font=small, fill="#1a1a1a")

    return img


def main() -> None:
    card = draw_card()

    upright = OUT / "card_upright.jpg"
    card.save(upright, quality=95)

    # Simulate a phone photo: rotate the PIXELS 90 degrees clockwise, then set
    # EXIF orientation = 6 ("rotate 90 CW to display"). A viewer shows it
    # upright; a naive decode sees it sideways. This is the case exif_transpose
    # exists to fix.
    sideways = card.transpose(Image.Transpose.ROTATE_90)
    exif = Image.Exif()
    exif[274] = 6  # 274 = Orientation tag
    rotated = OUT / "card_rotated.jpg"
    sideways.save(rotated, quality=95, exif=exif)

    for path in (upright, rotated):
        with Image.open(path) as im:
            tag = im.getexif().get(274)
            print(f"{path}  stored={im.size}  exif_orientation={tag}")


if __name__ == "__main__":
    main()
