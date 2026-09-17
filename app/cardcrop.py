"""
Find the card in a photograph and flatten it.

THE PROBLEM THIS SOLVES. A phone photo of a business card is mostly desk. In
the benchmark corpus the card covers roughly half the frame, so half of every
image sent to the model is wood grain -- and the model charges the same for
wood grain as for a phone number. Cropping to the card raises the effective
text size at any given output resolution, which is the thing that actually
governs whether an 8pt email address survives downscaling.

THE RULE THIS MODULE IS BUILT AROUND: IT MUST NEVER MAKE THINGS WORSE.
Every failure path returns the original image unchanged. A card detector that
occasionally returns a confident crop of the wrong rectangle is far worse than
one that often declines, because a wrong crop silently deletes the half of the
card with the phone number on it and the model dutifully reports what is left.
So the checks below are deliberately conservative: four points, convex,
plausible area, plausible aspect ratio. Anything else, and we hand back what we
were given.

WHY OPENCV. The pipeline is grayscale -> blur -> Canny -> findContours ->
approxPolyDP -> getPerspectiveTransform -> warpPerspective. Pillow has no
equivalent of the last two: a perspective warp needs the 3x3 homography solved
from four point correspondences, and Pillow's transform cannot derive it.
opencv-python-headless rather than opencv-python because the full package
depends on GUI libraries (X11, GTK) that are not present in a slim container
and would not be used if they were.
"""

import cv2
import numpy as np
from PIL import Image

# --- Thresholds, each chosen for a reason rather than tuned until it worked --

# The quad must cover at least this fraction of the frame. Below it we are
# almost certainly looking at a logo, a business-card-shaped shadow, or a
# rectangle printed ON the card rather than the card itself.
MIN_AREA_FRACTION = 0.10

# ...and at most this. A quad covering essentially the whole frame is the image
# border, which findContours reports happily and which crops away nothing while
# still costing a warp. Rejecting it means "no card found", which is correct.
MAX_AREA_FRACTION = 0.97

# A business card is 3.5x2 inches (1.75) or 85x55mm (1.55). Tilt and
# perspective stretch that, so the window is generous -- but a 4:1 sliver or a
# near-square is not a card seen at an angle, it is something else.
MIN_ASPECT, MAX_ASPECT = 1.15, 2.60

# Expand the detected quad outward by this fraction before warping.
#
# ADDED AFTER A MEASURED FAILURE, not on principle. On a creased card
# photographed against patterned fabric (samples/real/cl4.jpg), Canny traced a
# contour slightly INSIDE the card's true edge, and the warp clipped the last
# line of text -- the city -- clean off. The model then confidently reported
# everything except the location, and nothing about the output looked wrong.
#
# The two directions of error are not equally bad. Overshoot and the crop
# includes a sliver of desk, which costs nothing measurable: the model
# normalises the image internally, so a few percent of extra background does
# not change the token count at all. Undershoot and text is destroyed before
# the model ever sees it, with no signal that it happened. So the tie is broken
# outward, deliberately.
QUAD_PAD_FRACTION = 0.025

# Work at this width for detection. Contour finding does not need full
# resolution and an 8 MP image makes Canny meaningfully slower for no gain;
# the corners found here are scaled back up before the warp, so the final crop
# is still cut from the original pixels at full quality.
DETECT_WIDTH = 900


def _order_corners(points: np.ndarray) -> np.ndarray:
    """
    Put four corners in a known order: top-left, top-right, bottom-right, bottom-left.

    findContours returns them in traversal order, which may start anywhere and
    may run either way round. Feeding that straight to getPerspectiveTransform
    produces a card that is rotated or mirrored -- and a mirrored card still
    looks like a card, so the bug survives a glance at the output.

    The trick: for the TL corner x+y is smallest and for BR it is largest;
    for TR the difference x-y is largest and for BL smallest. True for any
    convex quadrilateral regardless of rotation up to 45 degrees.
    """
    ordered = np.zeros((4, 2), dtype=np.float32)
    total = points.sum(axis=1)
    diff = np.diff(points, axis=1).ravel()
    ordered[0] = points[np.argmin(total)]   # top-left
    ordered[2] = points[np.argmax(total)]   # bottom-right
    ordered[1] = points[np.argmin(diff)]    # top-right
    ordered[3] = points[np.argmax(diff)]    # bottom-left
    return ordered


def _find_card_quad(bgr: np.ndarray) -> np.ndarray | None:
    """The card's four corners in this image, or None if nothing plausible."""
    height, width = bgr.shape[:2]
    frame_area = float(height * width)

    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    # Blur before Canny: edge detection amplifies noise, and a noisy low-light
    # photo without this produces thousands of tiny contours and no card.
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)

    # Otsu picks the high threshold from the image's own histogram rather than
    # a constant, which is what lets the same code handle a white card on a
    # light desk and a dark card in low light. The canonical 0.5x ratio for the
    # low threshold comes from Canny's own recommendation.
    high, _ = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    edges = cv2.Canny(blurred, high * 0.5, high)

    # Close one-pixel gaps in the card's outline. A card edge interrupted by a
    # highlight becomes two contours instead of one closed quad, and the whole
    # detection fails on an otherwise perfect photo.
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
    edges = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, kernel)

    contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)

    for contour in sorted(contours, key=cv2.contourArea, reverse=True)[:12]:
        area = cv2.contourArea(contour)
        if not (frame_area * MIN_AREA_FRACTION < area < frame_area * MAX_AREA_FRACTION):
            continue

        # approxPolyDP simplifies the outline until it is a polygon. The
        # tolerance is a fraction of the contour's own perimeter, not a fixed
        # pixel count, so it scales with the card's size in frame.
        perimeter = cv2.arcLength(contour, True)
        approx = cv2.approxPolyDP(contour, 0.02 * perimeter, True)
        if len(approx) != 4 or not cv2.isContourConvex(approx):
            continue

        quad = _order_corners(approx.reshape(4, 2).astype(np.float32))
        widths = (np.linalg.norm(quad[1] - quad[0]), np.linalg.norm(quad[2] - quad[3]))
        heights = (np.linalg.norm(quad[3] - quad[0]), np.linalg.norm(quad[2] - quad[1]))
        long_edge, short_edge = max(widths), max(heights)
        if short_edge < 1:
            continue
        aspect = long_edge / short_edge
        # Accept either orientation: a portrait photo of a landscape card is
        # still a card, and exif_transpose has already run upstream.
        if not (MIN_ASPECT <= aspect <= MAX_ASPECT
                or MIN_ASPECT <= 1 / aspect <= MAX_ASPECT):
            continue
        return quad
    return None


def crop_to_card(image: Image.Image) -> Image.Image:
    """
    Return the card, flattened -- or the original image if none was found.

    Never raises. This runs inside the extraction pipeline, where an exception
    would turn a readable card into a failed row; declining to crop costs some
    tokens, and that is always the better trade.
    """
    try:
        bgr = cv2.cvtColor(np.array(image), cv2.COLOR_RGB2BGR)
        full_height, full_width = bgr.shape[:2]

        scale = DETECT_WIDTH / full_width if full_width > DETECT_WIDTH else 1.0
        small = (
            cv2.resize(bgr, (int(full_width * scale), int(full_height * scale)),
                       interpolation=cv2.INTER_AREA)
            if scale < 1.0 else bgr
        )

        quad = _find_card_quad(small)
        if quad is None:
            return image

        # Back to original coordinates, so the warp samples full-resolution
        # pixels rather than the downscaled detection copy.
        quad = quad / scale

        # Push each corner away from the centre. Scaling about the centroid
        # keeps the quad's shape and angle, so the perspective correction is
        # unaffected -- only the boundary moves outward.
        centre = quad.mean(axis=0)
        quad = centre + (quad - centre) * (1.0 + QUAD_PAD_FRACTION)
        # Padding can push corners outside the frame. warpPerspective samples
        # those as black rather than failing, which would put a dark band along
        # the card edge; clamping keeps every sample inside real pixels.
        quad[:, 0] = np.clip(quad[:, 0], 0, full_width - 1)
        quad[:, 1] = np.clip(quad[:, 1], 0, full_height - 1)

        widths = (np.linalg.norm(quad[1] - quad[0]), np.linalg.norm(quad[2] - quad[3]))
        heights = (np.linalg.norm(quad[3] - quad[0]), np.linalg.norm(quad[2] - quad[1]))
        # max, not mean: under perspective the near edge is longer, and
        # averaging would squash the near half of the card.
        out_w, out_h = int(max(widths)), int(max(heights))
        if out_w < 80 or out_h < 50:
            return image

        target = np.array(
            [[0, 0], [out_w - 1, 0], [out_w - 1, out_h - 1], [0, out_h - 1]],
            dtype=np.float32,
        )
        matrix = cv2.getPerspectiveTransform(quad, target)
        warped = cv2.warpPerspective(bgr, matrix, (out_w, out_h),
                                     flags=cv2.INTER_CUBIC)
        return Image.fromarray(cv2.cvtColor(warped, cv2.COLOR_BGR2RGB))
    except Exception:
        # Deliberately broad. Every OpenCV failure mode here -- a degenerate
        # homography, an empty contour set, an unexpected channel count -- has
        # the same correct response: hand back the image we were given.
        return image
