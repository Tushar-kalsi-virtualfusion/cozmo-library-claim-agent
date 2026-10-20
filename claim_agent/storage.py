"""Sweep-local immutable evidence plus atomic snapshots. No shared database."""
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from uuid import uuid4

from .models import Evidence, Sweep, SweepState


def atomic_write(path: Path, content: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class EvidenceStore:
    def __init__(self, root: Path, *, blob_root: Path | None = None):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.blob_root = Path(blob_root).resolve() if blob_root else self.root / ".image-blobs"

    def directory(self, sweep_id: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{32}", sweep_id):
            raise ValueError("Invalid sweep id")
        return self.root / sweep_id

    def evidence_path(self, sweep_id: str, ref: str) -> Path:
        if not re.fullmatch(r"(frames|depth|geometry|sources|observations)/[A-Za-z0-9_-]+\.[a-z0-9]+", ref):
            raise ValueError("Invalid evidence reference")
        base = self.directory(sweep_id)
        path = base / ref
        if not path.resolve().is_relative_to(base.resolve()):
            raise ValueError("Evidence path escapes sweep")
        return path

    def create(self, device: str, country: str, currency: str, synthetic=False) -> SweepState:
        state = SweepState(sweep=Sweep(id=uuid4().hex, device=device, country=country, currency=currency, synthetic=synthetic))
        self.save(state)
        return state

    def load(self, sweep_id: str) -> SweepState:
        return SweepState.model_validate_json((self.directory(sweep_id) / "state.json").read_bytes())

    def save(self, state: SweepState):
        atomic_write(self.directory(state.sweep.id) / "state.json", state.model_dump_json(indent=2).encode())

    def put_evidence(self, state: SweepState, ref: str, data: bytes) -> Evidence:
        path = self.evidence_path(state.sweep.id, ref)
        digest = hashlib.sha256(data).hexdigest()
        if path.exists():
            if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise ValueError("Immutable evidence id already has different bytes")
        else:
            if ref.startswith("frames/"):
                # One physical copy per JPEG hash, including independent comparisons.
                # Metadata, geometry, recognition and costs remain sweep-local.
                blob = self.blob_root / (digest+".jpg")
                if not blob.exists():
                    atomic_write(blob, data)
                elif hashlib.sha256(blob.read_bytes()).hexdigest() != digest:
                    raise ValueError("Content-addressed image is corrupt")
                path.parent.mkdir(parents=True,exist_ok=True)
                try:
                    os.link(blob,path)
                except FileExistsError:
                    if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                        raise ValueError("Immutable evidence id already has different bytes")
            else:
                atomic_write(path, data)
        existing = state.evidence.get(ref)
        if existing and existing.sha256 != digest:
            raise ValueError("Evidence manifest conflict")
        record = existing or Evidence(sha256=digest, size=len(data))
        state.evidence[ref] = record
        return record

    def put_json(self, state: SweepState, ref: str, value: dict):
        return self.put_evidence(state, ref, json.dumps(value, sort_keys=True, allow_nan=False).encode())

    def read_evidence(self, state: SweepState, ref: str) -> bytes:
        record = state.evidence.get(ref)
        if record is None:
            raise ValueError("Unknown evidence reference")
        data = self.evidence_path(state.sweep.id, ref).read_bytes()
        if hashlib.sha256(data).hexdigest() != record.sha256:
            raise ValueError("Evidence hash mismatch")
        return data
