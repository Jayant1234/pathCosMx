#!/usr/bin/env python3
"""Seal a slide pack with a password, so the real data can ship inside the app (and a public repository).

  python pack/seal.py packs/hnsc_slide1 sealed/hnsc_slide1.pqc               # asks for the password twice
  python pack/seal.py packs/hnsc_slide1 sealed/hnsc_slide1.pqc --password-file ~/secret.txt

File: b"PQCSEAL1" + header length (4 bytes, big-endian) + header JSON + AES-256-GCM ciphertext of a tar of
cells.tsv.gz and manifest.json. The key comes from the password via scrypt (n=2^17, r=8: ~0.5 s and 128 MB per
guess). The header (pack name, dataset line, cell and section counts, KDF parameters) is readable without the
password and is authenticated with the ciphertext; nothing else is. A wrong password and a damaged file both fail
the GCM check. The app unlocks with unseal() into the user's PathQC/packs folder (server/shell.py).
"""
import argparse
import base64
import getpass
import hashlib
import io
import json
import os
import shutil
import sys
import tarfile
from pathlib import Path

MAGIC = b"PQCSEAL1"
FILES = ("cells.tsv.gz", "manifest.json")
KDF = {"kdf": "scrypt", "n": 2 ** 17, "r": 8, "p": 1}


class WrongPassword(ValueError):
    pass


def _key(password, salt, k):
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=k["n"], r=k["r"], p=k["p"], maxmem=2 ** 28, dklen=32)


def seal(pack, out, password):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    pack, out = Path(pack), Path(out)
    m = json.loads((pack / "manifest.json").read_text())
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        for f in FILES:
            t.add(pack / f, arcname=f)
    plain = buf.getvalue()
    salt, nonce = os.urandom(16), os.urandom(12)
    head = json.dumps({"format": 1, "name": m.get("pack", pack.name), "dataset": m.get("dataset", ""),
                       "n_cells": m.get("n_cells"), "sections": m.get("sections", []),
                       "id": hashlib.sha256(plain).hexdigest()[:16],
                       **KDF, "salt": base64.b64encode(salt).decode(), "nonce": base64.b64encode(nonce).decode()}).encode()
    ct = AESGCM(_key(password, salt, KDF)).encrypt(nonce, plain, head)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(MAGIC + len(head).to_bytes(4, "big") + head + ct)
    return json.loads(head)


def header(f):
    """-> (header dict, header bytes, ciphertext). Raises ValueError on a file that is not a sealed pack."""
    b = Path(f).read_bytes()
    if b[:8] != MAGIC:
        raise ValueError(f"{f} is not a sealed PathQC pack")
    n = int.from_bytes(b[8:12], "big")
    raw = b[12:12 + n]
    return json.loads(raw), raw, b[12 + n:]


def unseal(f, password, packs_dir):
    """Decrypt `f` into packs_dir/<name>/ -> that folder. Raises WrongPassword. An existing folder of that name
    keeps its sessions and results; only the sealed files are replaced."""
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    h, raw, ct = header(f)
    try:
        plain = AESGCM(_key(password, base64.b64decode(h["salt"]), h)).decrypt(base64.b64decode(h["nonce"]), ct, raw)
    except InvalidTag:
        raise WrongPassword("wrong password")
    name = Path(h["name"]).name
    if not name or name.startswith("."):
        raise ValueError("bad pack name in the sealed file")
    dest = Path(packs_dir) / name
    tmp = Path(packs_dir) / f".{name}.unsealing"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(plain)) as t:
        for mem in t.getmembers():
            if mem.name not in FILES or not mem.isfile():
                raise ValueError(f"unexpected entry {mem.name!r} in the sealed file")
            (tmp / mem.name).write_bytes(t.extractfile(mem).read())
    dest.mkdir(parents=True, exist_ok=True)
    for f_ in FILES:
        os.replace(tmp / f_, dest / f_)
    (dest / ".sealed_id").write_text(h["id"])
    shutil.rmtree(tmp, ignore_errors=True)
    return dest


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pack")
    ap.add_argument("out")
    ap.add_argument("--password-file", help="read the password from this file (first line)")
    a = ap.parse_args()
    if a.password_file:
        pw = Path(a.password_file).expanduser().read_text().splitlines()[0].strip()
    else:
        pw = getpass.getpass("Password: ")
        if pw != getpass.getpass("Again: "):
            sys.exit("passwords differ")
    if len(pw) < 12:
        sys.exit("use a password of 12 characters or more")
    h = seal(a.pack, a.out, pw)
    print(f"sealed {h['name']} ({h['n_cells']} cells, id {h['id']}) -> {a.out} ({Path(a.out).stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
