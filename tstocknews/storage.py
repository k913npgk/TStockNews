"""Atomic JSON state; files survive Actions through a dedicated data branch."""
import hashlib
import json
import os
import gzip
from pathlib import Path


def read(path, default=None):
    path = Path(path)
    if not path.exists():
        return default
    text = gzip.decompress(path.read_bytes()).decode("utf-8") if path.suffix == ".gz" else path.read_text(encoding="utf-8")
    return json.loads(text)


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    text = json.dumps(value, ensure_ascii=False, sort_keys=True,
                      indent=None if path.suffix == ".gz" else 2, allow_nan=False) + "\n"
    if path.suffix == ".gz":
        temporary.write_bytes(gzip.compress(text.encode("utf-8"), mtime=0))
    else:
        temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     allow_nan=False).encode()).hexdigest()
