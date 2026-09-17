"""
Image preprocessing performed BEFORE anything is sent to the model.

This is the most under-appreciated part of a VLM pipeline. Two operations
happen here, and both materially change extraction accuracy:

1. EXIF ORIENTATION IS APPLIED (and then discarded).
   Phone cameras almost always write pixels in the sensor's native landscape
   order and record "rotate this 90 deg when displaying" as an EXIF tag. Photo
   viewers honour that tag, so the photo LOOKS upright to the human who took
   it. But a raw decode ignores the tag, so the model receives a sideways
   card. Text recognition is extremely orientation-sensitive -- a VLM reading
   a 90-degree-rotated card typically returns garbage or nulls. So the single
   highest-value line in this file is exif_transpose(): it bakes the rotation
   into the actual pixels, guaranteeing the model sees what the human saw.

2. THE IMAGE IS DOWNSCALED TO A MAX LONG EDGE (default 1024px).
   A VLM does not "look at" an image; it chops it into fixed-size patches and
   turns each patch into a token that flows through the transformer alongside
   the text. Token count scales with PIXEL AREA, so cost is quadratic in edge
   length. A 4032x3024 phone photo is ~12x the area of a 1024x768 one, i.e.
   roughly 12x the vision tokens. Consequences:
     - Latency: on a CPU-hosted 2B model this is the difference between
       "a few seconds" and "minutes per card".
     - Memory: vision tokens occupy the KV cache; a big enough image can
       exceed the context window and the request simply fails.
     - Accuracy does NOT improve to match. Most VLMs (Qwen2.5-VL/Qwen3-VL
       included) resize internally to their own supported resolution anyway.
       If we send 4032px, the server downscales it with whatever resampler it
       happens to use -- so we pay full upload and preprocessing cost to end
       up at a similar resolution regardless.
   Doing the resize ourselves with a high-quality filter (LANCZOS) gives a
   sharper result than a naive server-side resize, keeps behaviour IDENTICAL
   across Ollama / llama.cpp / vLLM, and makes latency predictable.

   Why 1024 and not 512? Business cards have small print -- 8pt phone numbers
   and emails. Below ~1024 the long edge, digits start to blur together and
   the model misreads or omits them. 1024 is the practical floor for reliable
   small-text reading; it is exposed as MAX_IMAGE_EDGE so it stays tunable.

We deliberately NEVER upscale: enlarging a small image adds no information,
only tokens and latency.
"""

import base64
import io

from PIL import Image, ImageOps, UnidentifiedImageError

from app.config import settings

# HEIC/HEIF support.
#
# This is not optional polish for this app: HEIC is the DEFAULT camera format
# on every iPhone since iOS 11, and photographing business cards with a phone
# is the entire use case. Pillow has no built-in HEIC decoder, so without this
# registration every straight-from-an-iPhone upload fails with "cannot
# identify image file" -- the most likely real-world input, rejected.
#
# Imported defensively: if the wheel is missing on some platform the app still
# starts and simply does not accept HEIC, rather than refusing to boot.
try:
    import pillow_heif

    pillow_heif.register_heif_opener()
    HEIC_SUPPORTED = True
except ImportError:  # pragma: no cover - depends on the install environment
    HEIC_SUPPORTED = False

# Guard against "decompression bombs": a tiny file that declares enormous
# dimensions (a 2 KB PNG claiming 50000x50000 would allocate ~7.5 GB on decode
# and kill the process). Pillow raises DecompressionBombError past this limit,
# which we catch below and turn into an ordinary rejected upload.
Image.MAX_IMAGE_PIXELS = settings.max_image_pixels


class ImageProcessingError(Exception):
    """Raised when the uploaded bytes are not a usable image."""


def to_data_url(jpeg_bytes: bytes) -> str:
    """Wrap already-normalised JPEG bytes as a data URL for the model."""
    encoded = base64.b64encode(jpeg_bytes).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"


def preprocess_to_data_url(raw_bytes: bytes) -> str:
    """
    Turn arbitrary uploaded image bytes into a normalised JPEG data URL.

    Returns a string of the form "data:image/jpeg;base64,<...>", which is the
    format the OpenAI-compatible image_url content block expects. Ollama,
    llama.cpp and vLLM all accept this, which is why we use it rather than any
    server-specific image field.

    Kept as a one-liner over preprocess_to_jpeg so that existing callers --
    and /api/model-check, which has no image to retain -- are unchanged.
    """
    return to_data_url(preprocess_to_jpeg(raw_bytes))


def preprocess_to_jpeg(raw_bytes: bytes) -> bytes:
    """
    Normalise uploaded image bytes to JPEG. THE function; everything else wraps it.

    WHY THIS WAS SPLIT OUT OF preprocess_to_data_url. Image retention needs the
    raw JPEG bytes: to hash them for the content-addressed filename, and to
    write them to disk. Going through the data URL would mean base64-encoding
    (+33% size) and then immediately decoding again, per card, to recover bytes
    this function already had. The split costs one extra function and makes the
    encode happen exactly once, at the point that actually needs it.

    It also fixes what gets STORED. The bytes retained on disk are now
    guaranteed to be byte-identical to the bytes the model saw -- same
    rotation, same resize, same quantisation tables. When a user disputes an
    extraction, the image they are shown is the evidence, not a re-derivation
    of it that might differ.
    """
    try:
        image = Image.open(io.BytesIO(raw_bytes))
        # Pillow is lazy: open() only reads the header. load() forces a full
        # decode now, so a truncated/corrupt file fails HERE, inside our
        # try/except, rather than unpredictably later during resize or save.
        image.load()
    except Image.DecompressionBombError as exc:
        raise ImageProcessingError(f"image is suspiciously large: {exc}") from exc
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ImageProcessingError(f"not a readable image: {exc}") from exc

    # --- 1. Apply EXIF rotation to the pixels themselves ---------------------
    # Returns a new image with the rotation baked in and the now-meaningless
    # orientation tag stripped, so nothing can double-rotate it downstream.
    image = ImageOps.exif_transpose(image)

    # --- 2. Normalise the colour mode ---------------------------------------
    # Uploads arrive as RGBA (PNG screenshots), P (palette GIF), LA, CMYK
    # (print-shop scans) or 1-bit. JPEG cannot store alpha or palette modes and
    # save() would raise. Converting to RGB makes the rest of the path
    # total-function: any input mode, one output mode.
    if image.mode != "RGB":
        image = image.convert("RGB")

    # --- 3. Downscale so the long edge is at most max_image_edge -------------
    max_edge = settings.max_image_edge
    width, height = image.size
    longest = max(width, height)
    if longest > max_edge:
        scale = max_edge / longest
        new_size = (max(1, round(width * scale)), max(1, round(height * scale)))
        # LANCZOS is the highest-quality downsampling filter Pillow offers.
        # It preserves the sharp edges of small printed text far better than
        # NEAREST/BILINEAR, which is exactly what we need the model to read.
        image = image.resize(new_size, Image.Resampling.LANCZOS)

    # --- 4. Re-encode as JPEG ------------------------------------------------
    buffer = io.BytesIO()
    # quality=90 is visually near-lossless for text while being far smaller
    # than PNG. subsampling=0 disables chroma subsampling: the default 4:2:0
    # halves colour resolution, which smears coloured text on coloured
    # backgrounds -- common on business cards.
    image.save(buffer, format="JPEG", quality=90, subsampling=0, optimize=True)
    return buffer.getvalue()


def describe(raw_bytes: bytes) -> dict:
    """Small helper used by tests/diagnostics to show what preprocessing did."""
    before = Image.open(io.BytesIO(raw_bytes))
    before_size = before.size
    data_url = preprocess_to_data_url(raw_bytes)
    payload = data_url.split(",", 1)[1]
    after = Image.open(io.BytesIO(base64.b64decode(payload)))
    return {
        "original_size": before_size,
        "original_bytes": len(raw_bytes),
        "processed_size": after.size,
        "processed_bytes": len(base64.b64decode(payload)),
    }
