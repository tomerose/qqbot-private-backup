"""Short-lived image-edit state scoped to one QQ conversation.

AstrBot's attachment paths are temporary.  Copying the image here lets a user
send an image first and the edit instruction in the next message without
turning a transient host path into long-term memory.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import time
from dataclasses import dataclass
from pathlib import Path

from PIL import Image as PillowImage


@dataclass(frozen=True)
class EditSession:
    image_b64: str | None = None
    intent_kind: str | None = None
    intent_prompt: str | None = None


class ImageEditSessionStore:
    """Persist one expiring image and intent per hashed conversation scope."""

    def __init__(
        self,
        root: Path,
        *,
        ttl_seconds: int = 30 * 60,
        max_image_bytes: int = 20 * 1024 * 1024,
        max_image_edge: int = 4096,
    ) -> None:
        self.root = Path(root)
        self.ttl_seconds = max(60, int(ttl_seconds))
        self.max_image_bytes = int(max_image_bytes)
        self.max_image_edge = int(max_image_edge)

    @staticmethod
    def _key(scope: str) -> str:
        return hashlib.sha256(str(scope or "").encode("utf-8")).hexdigest()[:32]

    def _paths(self, scope: str) -> tuple[Path, Path]:
        key = self._key(scope)
        return self.root / f"{key}.json", self.root / f"{key}.png"

    def _read_meta(self, scope: str) -> dict:
        meta_path, _ = self._paths(scope)
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
            updated_at = float(data.get("updated_at", 0))
            if time.time() - updated_at > self.ttl_seconds:
                self.clear(scope)
                return {}
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return {}

    def _write_meta(self, scope: str, data: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        meta_path, _ = self._paths(scope)
        data = {**data, "updated_at": time.time()}
        tmp = meta_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(meta_path)

    def remember_image(self, scope: str, image_b64: str) -> None:
        data = self._read_meta(scope)
        payload = base64.b64decode(str(image_b64 or ""), validate=True)
        if not payload or len(payload) > self.max_image_bytes:
            raise ValueError("invalid image size")
        with PillowImage.open(io.BytesIO(payload)) as source:
            source.load()
            if not (1 <= source.width <= self.max_image_edge):
                raise ValueError("invalid image width")
            if not (1 <= source.height <= self.max_image_edge):
                raise ValueError("invalid image height")
            normalized = source.convert("RGBA" if "A" in source.getbands() else "RGB")
            self.root.mkdir(parents=True, exist_ok=True)
            _, image_path = self._paths(scope)
            tmp = image_path.with_suffix(".png.tmp")
            with tmp.open("wb") as handle:
                normalized.save(handle, format="PNG", optimize=True)
            tmp.replace(image_path)
        self._write_meta(scope, data)

    def remember_intent(self, scope: str, kind: str, prompt: str) -> None:
        if kind not in {"edit", "dewatermark"}:
            raise ValueError("unsupported edit intent")
        data = self._read_meta(scope)
        data.update({"intent_kind": kind, "intent_prompt": str(prompt or "")[:500]})
        self._write_meta(scope, data)

    def get(self, scope: str) -> EditSession:
        data = self._read_meta(scope)
        if not data:
            return EditSession()
        _, image_path = self._paths(scope)
        image_b64 = None
        try:
            payload = image_path.read_bytes()
            if payload and len(payload) <= self.max_image_bytes:
                image_b64 = base64.b64encode(payload).decode("ascii")
        except OSError:
            pass
        return EditSession(
            image_b64=image_b64,
            intent_kind=data.get("intent_kind"),
            intent_prompt=data.get("intent_prompt"),
        )

    def clear(self, scope: str) -> None:
        for path in self._paths(scope):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    def cleanup(self) -> None:
        if not self.root.is_dir():
            return
        cutoff = time.time() - self.ttl_seconds
        for meta_path in self.root.glob("*.json"):
            try:
                data = json.loads(meta_path.read_text(encoding="utf-8"))
                if float(data.get("updated_at", 0)) >= cutoff:
                    continue
                image_path = meta_path.with_suffix(".png")
                meta_path.unlink(missing_ok=True)
                image_path.unlink(missing_ok=True)
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                try:
                    meta_path.unlink(missing_ok=True)
                    meta_path.with_suffix(".png").unlink(missing_ok=True)
                except OSError:
                    pass
