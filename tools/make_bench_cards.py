"""
Generate the benchmark corpus: distinct cards, each with exact ground truth.

WHY THIS EXISTS RATHER THAN REUSING samples/batch/.
Twelve of the thirteen cards in samples/batch/ are BYTE-IDENTICAL copies. They
were fine for exercising the job queue, and they are worthless for measuring
latency: Ollama caches the prompt prefix, so the second identical image returns
in ~22s against ~172s for the first. A benchmark built on them would report a
7x speedup that is entirely an artefact of the cache.

Every card here is therefore distinct in content, and the hard cases are
distinct in condition too.

WHY THE CARDS SIT ON A DESK.
Each image is a card photographed against a surface, with margin around it --
because that is what a phone photo of a business card actually is, and because
the margin is the thing card-detection (A3) removes. Rendering bare cards would
make the crop experiment measure nothing: there would be no desk to crop away,
and the result would look like the feature does not help.

GROUND TRUTH IS EXACT, NOT HAND-TRANSCRIBED.
The text is drawn from the same dict that is written to <card>.json, so the
expected values cannot drift from what is actually printed on the card. A
hand-written ground truth file is itself a source of error -- a typo in it
shows up as a model accuracy failure.
"""

import json
import pathlib
import random

from PIL import Image, ImageDraw, ImageFilter, ImageFont

OUT = pathlib.Path("samples/bench")

# Deterministic: the same corpus every run, so two benchmark runs are
# comparable and a regression is a real regression.
SEED = 20260917

CARDS = [
    {
        "id": "01_clean_serif",
        "first_name": "Asha", "last_name": "Rao",
        "title": "Head of Enterprise Sales", "company": "Nimbus Logistics",
        "location": "Mumbai", "phone": "+91 98200 12345",
        "email": "asha.rao@nimbuslogistics.in",
        "style": {"bg": "#fbfaf7", "fg": "#1b1c1e", "accent": "#7a1f2b"},
    },
    {
        "id": "02_clean_sans",
        "first_name": "Daniel", "last_name": "Okonkwo",
        "title": "Chief Technology Officer", "company": "Reva Analytics",
        "location": "Bengaluru", "phone": "+91 80 4123 9900",
        "email": "d.okonkwo@reva-analytics.com",
        "style": {"bg": "#ffffff", "fg": "#14181d", "accent": "#12507a"},
    },
    {
        "id": "03_dark_card",
        "first_name": "Mei", "last_name": "Tanaka",
        "title": "Procurement Manager", "company": "Kestrel Foods",
        "location": "Singapore", "phone": "+65 6221 4470",
        "email": "m.tanaka@kestrelfoods.sg",
        # Light text on a dark card: a genuinely different contrast regime,
        # and the case where aggressive downscaling hurts first.
        "style": {"bg": "#1d2024", "fg": "#f2f2f0", "accent": "#c8a04a"},
    },
    {
        "id": "04_dense",
        "first_name": "Priya", "last_name": "Venkatesh",
        "title": "Senior Regional Business Development Manager, South Asia",
        "company": "Harbourline Shipping & Freight Forwarding Pvt Ltd",
        "location": "Chennai, Tamil Nadu",
        "phone": "+91 44 2851 6620",
        "email": "priya.venkatesh@harbourline-shipping.co.in",
        # HARD CASE: long strings at small point size, plus decoy numbers.
        # This is the card that punishes a low resolution first.
        "style": {"bg": "#f4f4f2", "fg": "#20232a", "accent": "#2c6e49",
                  "dense": True},
    },
    {
        "id": "05_angled",
        "first_name": "Tomas", "last_name": "Lindqvist",
        "title": "Operations Lead", "company": "Vastra Nordic AB",
        "location": "Stockholm", "phone": "+46 8 559 21 400",
        "email": "tomas.lindqvist@vastranordic.se",
        # HARD CASE: photographed at an angle. This is the card that
        # perspective correction exists for.
        "style": {"bg": "#fdfdfb", "fg": "#191b1d", "accent": "#3a4a5a",
                  "angle": True},
    },
    {
        "id": "06_lowlight",
        "first_name": "Farah", "last_name": "Siddiqui",
        "title": "Account Director", "company": "Crescent Media Group",
        "location": "Dubai", "phone": "+971 4 388 2100",
        "email": "farah.s@crescentmedia.ae",
        # HARD CASE: underexposed with sensor noise, like an indoor phone shot.
        "style": {"bg": "#faf8f4", "fg": "#1c1d1f", "accent": "#8a5a2b",
                  "lowlight": True},
    },
]

FIELDS = ("first_name", "last_name", "title", "company",
          "location", "phone", "email")


def _font(size: int, bold: bool = False):
    for path in (
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf" if bold
        else "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold
        else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


def draw_card(card: dict) -> Image.Image:
    """The card itself, at a realistic 3.5x2 inch aspect ratio."""
    style = card["style"]
    dense = style.get("dense", False)
    w, h = 1050, 600
    img = Image.new("RGB", (w, h), style["bg"])
    d = ImageDraw.Draw(img)

    d.rectangle([0, 0, 14, h], fill=style["accent"])

    name = f"{card['first_name']} {card['last_name']}"
    if dense:
        d.text((60, 62), name, font=_font(52, bold=True), fill=style["fg"])
        d.text((60, 126), card["title"], font=_font(23), fill=style["fg"])
        d.text((60, 160), card["company"], font=_font(25, bold=True), fill=style["accent"])
        y = 232
        for line in (card["location"], card["phone"], card["email"],
                     "Reg. 29AAACH1234P1Z5", "Fax +91 44 2851 6621"):
            d.text((60, y), line, font=_font(21), fill=style["fg"])
            y += 34
    else:
        d.text((64, 96), name, font=_font(64, bold=True), fill=style["fg"])
        d.text((64, 176), card["title"], font=_font(30), fill=style["fg"])
        d.text((64, 232), card["company"], font=_font(34, bold=True), fill=style["accent"])
        y = 352
        for line in (card["location"], card["phone"], card["email"]):
            d.text((64, y), line, font=_font(27), fill=style["fg"])
            y += 46
    return img


def photograph(card_img: Image.Image, style: dict, rng: random.Random) -> Image.Image:
    """
    Place the card on a desk and shoot it, the way a phone would.

    The output is deliberately 2000px+ so that the resize path in imaging.py is
    actually exercised -- benchmarking an image that is already below
    MAX_IMAGE_EDGE would measure nothing about resolution.
    """
    W, H = 2200, 1650
    # A mottled desk surface. Uniform grey would make contour detection
    # unrealistically easy.
    desk = Image.new("RGB", (W, H), (150, 143, 132))
    noise = Image.effect_noise((W, H), 18).convert("L")
    desk = Image.composite(desk, Image.new("RGB", (W, H), (128, 121, 112)), noise)
    desk = desk.filter(ImageFilter.GaussianBlur(1.2))

    scale = 1.55
    card = card_img.resize(
        (int(card_img.width * scale), int(card_img.height * scale)),
        Image.Resampling.LANCZOS,
    )

    if style.get("angle"):
        # A real perspective tilt, not a rotation: the far edge is shorter.
        cw, ch = card.size
        pad = int(cw * 0.16)
        canvas = Image.new("RGB", (cw + pad * 2, ch + pad * 2), (150, 143, 132))
        canvas.paste(card, (pad, pad))
        cw, ch = canvas.size
        # PIL's PERSPECTIVE wants coefficients mapping OUTPUT back to INPUT.
        coeffs = _perspective_coeffs(
            [(0, 0), (cw, 0), (cw, ch), (0, ch)],
            [(int(cw * 0.10), int(ch * 0.04)), (int(cw * 0.95), int(ch * 0.14)),
             (int(cw * 0.88), int(ch * 0.97)), (int(cw * 0.04), int(ch * 0.86))],
        )
        card = canvas.transform((cw, ch), Image.Transform.PERSPECTIVE, coeffs,
                                Image.Resampling.BICUBIC,
                                fillcolor=(150, 143, 132))
    else:
        card = card.rotate(rng.uniform(-3.5, 3.5), expand=True,
                           resample=Image.Resampling.BICUBIC,
                           fillcolor=(150, 143, 132))

    desk.paste(card, ((W - card.width) // 2, (H - card.height) // 2))

    if style.get("lowlight"):
        # Underexpose, then add sensor noise -- in that order, because real
        # noise is amplified by the gain a dark scene forces.
        desk = Image.eval(desk, lambda v: int(v * 0.42))
        grain = Image.effect_noise((W, H), 26).convert("RGB")
        desk = Image.blend(desk, grain, 0.16)
        desk = desk.filter(ImageFilter.GaussianBlur(0.6))

    return desk


def _solve(matrix: list[list[float]], rhs: list[float]) -> list[float]:
    """
    Gauss-Jordan with partial pivoting. Eight unknowns, so this is instant.

    Written out rather than pulled from numpy deliberately: this generator is a
    development tool, and making the test corpus depend on a numerical stack
    means anyone regenerating it needs that stack installed. Partial pivoting
    (swapping in the largest available pivot row) is what keeps it stable --
    without it, a zero or tiny pivot divides the whole row into nonsense.
    """
    n = len(rhs)
    aug = [row[:] + [rhs[i]] for i, row in enumerate(matrix)]

    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(aug[r][col]))
        if abs(aug[pivot][col]) < 1e-12:
            raise ValueError("degenerate perspective quad")
        aug[col], aug[pivot] = aug[pivot], aug[col]

        divisor = aug[col][col]
        aug[col] = [value / divisor for value in aug[col]]

        for row in range(n):
            if row == col:
                continue
            factor = aug[row][col]
            if factor:
                aug[row] = [v - factor * p for v, p in zip(aug[row], aug[col])]

    return [aug[i][n] for i in range(n)]


def _perspective_coeffs(target, source):
    """
    The 8 coefficients PIL's PERSPECTIVE transform needs.

    Note the direction: PIL maps each OUTPUT pixel back to an INPUT pixel, so
    `target` is where the corners end up and `source` is where they came from.
    Getting this backwards produces a card warped the opposite way, which looks
    plausible enough to miss.
    """
    matrix, rhs = [], []
    for (tx, ty), (sx, sy) in zip(target, source):
        matrix.append([tx, ty, 1, 0, 0, 0, -sx * tx, -sx * ty])
        rhs.append(sx)
        matrix.append([0, 0, 0, tx, ty, 1, -sy * tx, -sy * ty])
        rhs.append(sy)
    return _solve(matrix, rhs)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    rng = random.Random(SEED)
    digests = {}

    for card in CARDS:
        image = photograph(draw_card(card), card["style"], rng)
        path = OUT / f"{card['id']}.jpg"
        # quality 88: a plausible phone JPEG, not a pristine one.
        image.save(path, format="JPEG", quality=88)

        truth = {field: card[field] for field in FIELDS}
        (OUT / f"{card['id']}.json").write_text(json.dumps(truth, indent=2) + "\n")

        import hashlib
        digest = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
        digests[card["id"]] = digest
        print(f"  {path}  {image.size[0]}x{image.size[1]}  "
              f"{path.stat().st_size // 1024} KB  sha={digest}")

    # THE CHECK THAT MATTERS: if any two cards are identical, every timing
    # taken after the first is a prompt-cache hit and the whole benchmark is
    # fiction. Fail here rather than produce confident wrong numbers.
    if len(set(digests.values())) != len(digests):
        raise SystemExit("FATAL: duplicate images in the corpus -- timings would be cached")
    print(f"\n{len(CARDS)} cards, all distinct. Ground truth written alongside.")


if __name__ == "__main__":
    main()
