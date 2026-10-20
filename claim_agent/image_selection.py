"""Conservative image-level dedup. Never infer physical object identity here."""
import hashlib
import io
import json

import numpy as np
from PIL import Image

from .vision import image_quality


def fingerprint(jpeg):
    with Image.open(io.BytesIO(jpeg)) as image:
        pixels = np.asarray(image.convert("L").resize((16,16), Image.Resampling.BILINEAR)).reshape(-1).tolist()
    return {"sha256":hashlib.sha256(jpeg).hexdigest(), "pixels":pixels, **image_quality(jpeg)}


def receipt(meta, jpeg, depth, confidence):
    digest = hashlib.sha256()
    for data in (json.dumps(meta.model_dump(), sort_keys=True).encode(), jpeg, depth, confidence):
        digest.update(len(data).to_bytes(8,"big")); digest.update(data)
    return digest.hexdigest()


def stationary(a, b):
    a, b = np.asarray(a).reshape((4,4), order="F"), np.asarray(b).reshape((4,4), order="F")
    # Approximately 2 cm and 2 degrees. Similar-looking objects at other poses remain separate.
    return np.linalg.norm(a[:3,3]-b[:3,3]) < .02 and np.trace(a[:3,:3].T @ b[:3,:3]) > 2.9987


def recognition_view_changed(metadata, image, old):
    """Coverage novelty is coarser than the pose limit for reusing geometry.

    Exposure and a one-cell thumbnail shift are not a new cloud opportunity.
    A substantial tracked viewpoint change still preserves coverage of similar
    looking objects in different positions; this never establishes identity.
    """
    if not image or not old.get("pixels"):
        return False
    if metadata and old.get("pose") and metadata.tracking == "normal" and old.get("tracking", "normal") == "normal":
        same_session = old.get("ar_session_id", metadata.ar_session_id) == metadata.ar_session_id
        a = np.asarray(metadata.camera_transform).reshape((4,4), order="F")
        b = np.asarray(old["pose"]).reshape((4,4), order="F")
        cosine = np.clip((np.trace(a[:3,:3].T @ b[:3,:3])-1)/2, -1, 1)
        if same_session and (np.linalg.norm(a[:3,3]-b[:3,3]) >= .25 or cosine <= np.cos(np.deg2rad(12))):
            return True
    current = np.asarray(image["pixels"], dtype=float).reshape((16,16))
    previous = np.asarray(old["pixels"], dtype=float).reshape((16,16))
    distances = []
    for row in (-1, 0, 1):
        for column in (-1, 0, 1):
            a = current[1:15, 1:15]
            b = previous[1+row:15+row, 1+column:15+column]
            difference = a-b
            distances.append(np.abs(difference-np.median(difference)).mean())
    return min(distances) >= 12


def duplicate_of(candidate, metadata, index, frames):
    for id, old in index.items():
        if old["sha256"] == candidate["sha256"]:
            return id
        previous = frames.get(id)
        if previous is None or metadata.tracking != "normal" or previous.tracking != "normal":
            continue
        if (metadata.image_width,metadata.image_height) != (previous.image_width,previous.image_height):
            continue
        if not stationary(metadata.camera_transform, previous.camera_transform):
            continue
        delta = np.abs(np.asarray(candidate["pixels"],dtype=float)-old["pixels"]).mean()
        if delta < 1.5 and candidate["sharpness"] <= old["sharpness"]*1.3:
            return id
    return None
