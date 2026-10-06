"""Avatar uploads.

Images are written to disk under `uploads/avatars/` and served as static files.
Only the path is stored on the user row — putting image bytes in the database
would bloat every `/auth/me` response, which the app makes on each page load.

Everything an upload endpoint should do before trusting a file happens here:
the bytes are decoded with Pillow (which rejects anything that is not really an
image, whatever the filename claims), the size is capped, the result is
re-encoded rather than passed through, and the filename is generated rather
than taken from the client.
"""

import uuid
from pathlib import Path

from fastapi import HTTPException, UploadFile, status
from PIL import Image, UnidentifiedImageError

# Relative to the backend working directory, and gitignored.
UPLOAD_ROOT = Path("uploads")
AVATAR_DIR = UPLOAD_ROOT / "avatars"

MAX_BYTES = 4 * 1024 * 1024  # 4 MB before decoding
OUTPUT_SIZE = 256  # square, enough for a 64px avatar on a retina screen
ALLOWED_FORMATS = {"PNG", "JPEG", "WEBP", "GIF"}


def _crop_square(img: Image.Image) -> Image.Image:
    """Centre-crop to a square so avatars never arrive stretched."""
    w, h = img.size
    if w == h:
        return img
    side = min(w, h)
    left = (w - side) // 2
    top = (h - side) // 2
    return img.crop((left, top, left + side, top + side))


async def save_avatar(file: UploadFile, user_id: uuid.UUID) -> str:
    """Validate, normalise and store an avatar. Returns the URL path to serve it."""
    raw = await file.read()

    if not raw:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="That file is empty."
        )
    if len(raw) > MAX_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"Images must be under {MAX_BYTES // (1024 * 1024)} MB.",
        )

    # Decoding is the real check. A .png extension proves nothing.
    try:
        import io

        img = Image.open(io.BytesIO(raw))
        img.verify()
        img = Image.open(io.BytesIO(raw))
    except (UnidentifiedImageError, OSError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="That does not look like an image file.",
        ) from exc

    if img.format not in ALLOWED_FORMATS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Use a PNG, JPEG, WEBP or GIF. That file is {img.format}.",
        )

    # Flatten transparency onto white; a PNG with alpha saved as JPEG goes black.
    img = _crop_square(img)
    img = img.convert("RGBA")
    background = Image.new("RGBA", img.size, (255, 255, 255, 255))
    background.alpha_composite(img)
    img = background.convert("RGB")
    img.thumbnail((OUTPUT_SIZE, OUTPUT_SIZE), Image.LANCZOS)

    AVATAR_DIR.mkdir(parents=True, exist_ok=True)

    # Filename is generated, never taken from the client, and a fresh uuid each
    # time so a replaced avatar is not served from a stale cache.
    name = f"{user_id.hex}-{uuid.uuid4().hex[:8]}.jpg"
    path = AVATAR_DIR / name

    # Re-encoding drops any EXIF, including GPS coordinates from a phone photo.
    img.save(path, format="JPEG", quality=86, optimize=True)

    return f"/uploads/avatars/{name}"


def delete_avatar(avatar_url: str | None) -> None:
    """Remove a previously stored avatar file, ignoring one that is already gone."""
    if not avatar_url or not avatar_url.startswith("/uploads/avatars/"):
        return
    name = Path(avatar_url).name
    # Rebuild the path from the basename only, so a crafted value cannot escape
    # the avatars directory.
    try:
        (AVATAR_DIR / name).unlink(missing_ok=True)
    except OSError:
        pass
