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
