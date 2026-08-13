from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass


MAX_REQUIREMENT_IMAGES = 5
MAX_REQUIREMENT_IMAGE_BYTES = 5 * 1024 * 1024
MAX_REQUIREMENT_IMAGES_TOTAL_BYTES = 15 * 1024 * 1024

MEDIA_TYPE_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
}


class RequirementAttachmentError(ValueError):
    pass


@dataclass(frozen=True)
class DecodedRequirementImage:
    filename: str
    media_type: str
    content: bytes


def decode_requirement_images(items: list[object]) -> list[DecodedRequirementImage]:
    if len(items) > MAX_REQUIREMENT_IMAGES:
        raise RequirementAttachmentError(
            f"a requirement can include at most {MAX_REQUIREMENT_IMAGES} images"
        )
    decoded: list[DecodedRequirementImage] = []
    total_bytes = 0
    for item in items:
        media_type = str(getattr(item, "media_type", ""))
        if media_type not in MEDIA_TYPE_EXTENSIONS:
            raise RequirementAttachmentError("unsupported requirement image type")
        try:
            content = base64.b64decode(str(getattr(item, "data_base64", "")), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise RequirementAttachmentError("requirement image is not valid base64") from exc
        if not content:
            raise RequirementAttachmentError("requirement image is empty")
        if len(content) > MAX_REQUIREMENT_IMAGE_BYTES:
            raise RequirementAttachmentError(
                f"each requirement image must be at most {MAX_REQUIREMENT_IMAGE_BYTES // 1024 // 1024} MB"
            )
        if not _matches_media_type(content, media_type):
            raise RequirementAttachmentError("requirement image content does not match its media type")
        total_bytes += len(content)
        if total_bytes > MAX_REQUIREMENT_IMAGES_TOTAL_BYTES:
            raise RequirementAttachmentError(
                f"requirement images must total at most {MAX_REQUIREMENT_IMAGES_TOTAL_BYTES // 1024 // 1024} MB"
            )
        decoded.append(
            DecodedRequirementImage(
                filename=_safe_filename(str(getattr(item, "filename", "")), media_type),
                media_type=media_type,
                content=content,
            )
        )
    return decoded


def _matches_media_type(content: bytes, media_type: str) -> bool:
    if media_type == "image/png":
        return content.startswith(b"\x89PNG\r\n\x1a\n")
    if media_type == "image/jpeg":
        return content.startswith(b"\xff\xd8\xff")
    if media_type == "image/webp":
        return len(content) >= 12 and content.startswith(b"RIFF") and content[8:12] == b"WEBP"
    return False


def _safe_filename(value: str, media_type: str) -> str:
    name = value.replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r"[\x00-\x1f\x7f]", "", name).strip().strip(".")
    extension = MEDIA_TYPE_EXTENSIONS[media_type]
    stem = name.rsplit(".", 1)[0].strip() if "." in name else name
    stem = stem[: 255 - len(extension)].strip() or "screenshot"
    return f"{stem}{extension}"
