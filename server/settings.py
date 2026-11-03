"""Per-user settings (reviewer, pack, H&E file + fingerprint, accepted registrations).

Stored as JSON in the user's PathQC folder: ~/Documents/PathQC/settings.json on macOS, Linux and Windows
(the same folder run.py uses for packs and the log). PATHQC_HOME overrides the folder (tests).
"""
import hashlib
import json
import os
import time
from pathlib import Path

HE_EXTENSIONS = (".svs", ".ndpi", ".tif", ".tiff", ".mrxs", ".scn", ".vms", ".vmu", ".bif", ".svslide", ".dcm")
HEAD, TAIL = 4 << 20, 1 << 20


def home():
    return Path(os.environ.get("PATHQC_HOME") or Path.home() / "Documents" / "PathQC")


def path():
    return home() / "settings.json"


def load():
    try:
        return json.loads(path().read_text())
    except (OSError, ValueError):
        return {}


def save(s):
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, indent=2))
    os.replace(tmp, p)


def fingerprint(f):
    """File size + sha256 of the first 4 MB and the last 1 MB: cheap on a multi-GB slide, and changes
    when the file is replaced. For .mrxs only the index file is fingerprinted (its data folder is not)."""
    f = Path(f)
    size = f.stat().st_size
    h = hashlib.sha256()
    with open(f, "rb") as fh:
        h.update(fh.read(HEAD))
        if size > HEAD:
            fh.seek(max(HEAD, size - TAIL))
            h.update(fh.read(TAIL))
    h.update(str(size).encode())
    return {"size": size, "sha256": h.hexdigest(), "id": h.hexdigest()[:16]}


def check_he(he):
    """-> (ok, problem). `he` is the saved {"path", "fingerprint"}."""
    if not he or not he.get("path"):
        return False, None
    f = Path(he["path"])
    if not f.is_file():
        return False, f"The H&E file is no longer at {f} (moved, renamed or on a disconnected drive)."
    fp = fingerprint(f)
    if fp["id"] != he.get("fingerprint", {}).get("id"):
        return False, f"The H&E file at {f} has changed since it was registered (different size or content)."
    return True, None


def pack_entry(s, pack):
    return s.setdefault("packs", {}).setdefault(str(Path(pack).resolve()), {})


def reg_key(pack, fp_id):
    return f"{Path(pack).resolve()}|{fp_id}"


def stamp():
    return time.strftime("%Y-%m-%dT%H:%M:%S")
