"""Strip solid or light backgrounds from uploaded logos."""

from collections import deque
from io import BytesIO
from pathlib import Path

import numpy as np
from django.core.files.base import ContentFile
from PIL import Image


def _border_background_mask(rgb: np.ndarray, tolerance: float) -> np.ndarray:
    """Mark background pixels connected to the image border."""
    height, width = rgb.shape[:2]
    edge_pixels = np.concatenate([
        rgb[0, :, :],
        rgb[-1, :, :],
        rgb[1:-1, 0, :],
        rgb[1:-1, -1, :],
    ])
    background = np.median(edge_pixels, axis=0)
    distance = np.sqrt(np.sum((rgb.astype(np.float32) - background) ** 2, axis=2))
    candidates = (distance <= tolerance) | np.all(rgb >= 245, axis=2)

    removable = np.zeros((height, width), dtype=bool)
    queue = deque()
    for x in range(width):
        if candidates[0, x]:
            queue.append((0, x))
        if candidates[height - 1, x]:
            queue.append((height - 1, x))
    for y in range(height):
        if candidates[y, 0]:
            queue.append((y, 0))
        if candidates[y, width - 1]:
            queue.append((y, width - 1))

    while queue:
        y, x = queue.popleft()
        if y < 0 or y >= height or x < 0 or x >= width:
            continue
        if removable[y, x] or not candidates[y, x]:
            continue
        removable[y, x] = True
        queue.extend([(y - 1, x), (y + 1, x), (y, x - 1), (y, x + 1)])

    return removable


def _has_transparency(alpha: np.ndarray) -> bool:
    return bool(np.any(alpha < 250))


def strip_logo_background(uploaded_file, tolerance: float = 40) -> ContentFile:
    """Return a trimmed PNG with the logo background removed."""
    uploaded_file.seek(0)
    image = Image.open(uploaded_file).convert("RGBA")
    data = np.array(image)
    alpha = data[:, :, 3]

    if not _has_transparency(alpha):
        removable = _border_background_mask(data[:, :, :3], tolerance)
        data[removable, 3] = 0

    result = Image.fromarray(data, "RGBA")
    bbox = result.getbbox()
    if bbox:
        result = result.crop(bbox)

    buffer = BytesIO()
    result.save(buffer, format="PNG", optimize=True)
    buffer.seek(0)
    stem = Path(getattr(uploaded_file, "name", "logo")).stem or "logo"
    return ContentFile(buffer.read(), name=f"{stem}.png")


MARK_SIZE = 256


def _logo_components(alpha: np.ndarray):
    """Yield (area, x0, y0, x1, y1) for every opaque blob in the logo."""
    from scipy import ndimage

    labels, count = ndimage.label(alpha > 0)
    for index, slc in enumerate(ndimage.find_objects(labels), start=1):
        if slc is None:
            continue
        ys, xs = slc
        area = int(np.count_nonzero(labels[slc] == index))
        yield area, xs.start, ys.start, xs.stop, ys.stop, (labels == index)


def build_logo_mark(logo_path, size: int = MARK_SIZE) -> Image.Image:
    """Return a square, anti-aliased icon cut from a wide logo (icon only, no wordmark).

    The icon is the left-most sizeable blob plus every blob that overlaps it
    horizontally and is not tiny; the wordmark and tagline text to the right are
    dropped.
    """
    image = Image.open(logo_path).convert("RGBA")
    data = np.array(image)
    alpha = data[:, :, 3]
    components = list(_logo_components(alpha))
    if not components:
        return image.resize((size, size), Image.LANCZOS)

    total = sum(c[0] for c in components)
    sizeable = [c for c in components if c[0] >= total * 0.01]
    seed = min(sizeable or components, key=lambda c: c[1])  # left-most sizeable blob
    cluster = [seed]
    x0, x1 = seed[1], seed[3]
    changed = True
    while changed:
        changed = False
        biggest = max(c[0] for c in cluster)
        for comp in components:
            if any(comp is c for c in cluster):
                continue
            overlaps = comp[1] < x1 and comp[3] > x0
            if overlaps and comp[0] >= biggest * 0.01:
                cluster.append(comp)
                x0, x1 = min(x0, comp[1]), max(x1, comp[3])
                changed = True

    keep = np.zeros_like(alpha, dtype=bool)
    for comp in cluster:
        keep |= comp[5]
    data[~keep, 3] = 0
    icon = Image.fromarray(data, "RGBA")
    icon = icon.crop(icon.getbbox() or (0, 0, image.width, image.height))

    # Square canvas with breathing room, downsampled with LANCZOS for smooth edges.
    side = int(max(icon.size) * 1.16)
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    canvas.paste(icon, ((side - icon.width) // 2, (side - icon.height) // 2), icon)
    return canvas.resize((size, size), Image.LANCZOS)


def logo_mark_url(logo_field):
    """Return the media URL of a cached square icon for the site logo, or None."""
    from django.conf import settings

    try:
        if not logo_field or not logo_field.name:
            return None
        source = Path(logo_field.path)
        if not source.exists():
            return None
        target = Path(settings.MEDIA_ROOT) / "branding" / "marks" / f"{source.stem}-mark.png"
        if not target.exists() or target.stat().st_mtime < source.stat().st_mtime:
            target.parent.mkdir(parents=True, exist_ok=True)
            build_logo_mark(source).save(target, format="PNG", optimize=True)
        relative = target.relative_to(settings.MEDIA_ROOT).as_posix()
        return f"{settings.MEDIA_URL}{relative}"
    except Exception:
        return None
