"""Per-reviewer review state: an append-only edit log plus comments, annotations and vocabulary.

Every geometry is stored in slide millimetres (the CosMx frame), never in image pixels, so the
work survives swapping the stand-in image for the registered H&E. Cell sets are stored as row
indices into the pack's cell table, fixed at the time of the edit; a whole-section op (start pick,
Restart, Clear) stores only its section names and is resolved against the pack at replay.

Corrected label of a cell = Bill_eugene_code, then every non-undone op in log order; the last op
touching the cell wins. A "clear" op puts its cells (a section, or all) back to untouched.
"""
import json
import re
import threading
import time
import uuid
from pathlib import Path

import numpy as np

TAG_DEFAULT, TAG_MANUAL = 0, 1
BASE_TAGS = {"bill_eugene_code": 2, "vestrum": 3, "claude_code": 4}
TAG_NAMES = {0: "bill_eugene_code-default", 1: "manual", 2: "bill_eugene_code-base",
             3: "vestrum-base", 4: "claude_code-base"}


def slug(name):
    s = re.sub(r"[^A-Za-z0-9_.-]+", "_", (name or "").strip())[:60].strip("._")
    return s or "anonymous"


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


class Session:
    def __init__(self, root: Path, reviewer: str, base_vocab, n_cells, section_cells=None):
        self.reviewer = reviewer
        self.section_cells = section_cells or {}      # section name -> cell indices
        self._idx = {}                                 # op id or sections -> cell indices (cached)
        self.dir = root / slug(reviewer)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.n = n_cells
        self.ops = self._read_jsonl("ops.jsonl")
        self.comments = self._read_json("comments.json", [])
        self.annotations = self._read_json("annotations.json", [])
        self.vocab = self._read_json("vocab.json", None) or [dict(v, merged_into=None) for v in base_vocab]
        self.vocab_log = self._read_json("vocab_log.json", [])

    # ---------- persistence
    def _read_json(self, name, default):
        p = self.dir / name
        return json.loads(p.read_text()) if p.exists() else default

    def _read_jsonl(self, name):
        p = self.dir / name
        if not p.exists():
            return []
        return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]

    def _write(self, name, obj):
        tmp = self.dir / (name + ".tmp")
        tmp.write_text(json.dumps(obj, indent=1))
        tmp.replace(self.dir / name)

    def _append_op(self, op):
        with open(self.dir / "ops.jsonl", "a") as fh:
            fh.write(json.dumps(op) + "\n")
        self.ops.append(op)

    # ---------- vocabulary
    def resolve_table(self):
        """code -> root code after merges (array indexed by code)."""
        by_code = {v["code"]: v for v in self.vocab}
        size = max(by_code) + 1
        table = np.arange(max(size, 256), dtype=np.int32)
        for code in by_code:
            seen, c = set(), code
            while by_code[c].get("merged_into") is not None and c not in seen:
                seen.add(c)
                c = by_code[c]["merged_into"]
            table[code] = c
        return table

    def vocab_action(self, a):
        by_code = {v["code"]: v for v in self.vocab}
        kind = a.get("action")
        if kind == "rename":
            name = a["name"].strip()
            if not name or any(v["name"] == name and v["code"] != a["code"] for v in self.vocab):
                raise ValueError("name is empty or already used")
            by_code[a["code"]]["name"] = name
        elif kind == "recolor":
            by_code[a["code"]]["color"] = a["color"]
        elif kind == "merge":
            src, dst = a["code"], a["into"]
            if src == dst or self.resolve_table()[dst] == src:
                raise ValueError("cannot merge a label into itself")
            by_code[src]["merged_into"] = dst
        elif kind == "unmerge":
            by_code[a["code"]]["merged_into"] = None
        elif kind == "add":
            name = a["name"].strip()
            if not name or any(v["name"] == name for v in self.vocab):
                raise ValueError("name is empty or already used")
            code = max(by_code) + 1
            if code > 250:
                raise ValueError("vocabulary is full")
            self.vocab.append({"code": code, "name": name, "color": a.get("color", "#e377c2"),
                               "merged_into": None, "added_by": self.reviewer})
        else:
            raise ValueError(f"unknown vocab action {kind}")
        self.vocab_log.append(dict(a, ts=now()))
        self._write("vocab.json", self.vocab)
        self._write("vocab_log.json", self.vocab_log)

    # ---------- edit log
    def active_ops(self):
        undone = {o["target"] for o in self.ops if o["op"] == "undo"}
        return [o for o in self.ops if o["op"] in ("base", "relabel", "clear") and o["id"] not in undone]

    def op_cells(self, o):
        """Cell indices of an op: its stored list, or the cells of its sections."""
        key = o["id"] if "cells" in o else tuple(o["sections"])
        idx = self._idx.get(key)
        if idx is None:
            if "cells" in o:
                idx = np.asarray(o["cells"], dtype=np.int64)
            else:
                idx = np.concatenate([self.section_cells[s] for s in o["sections"] if s in self.section_cells]
                                     or [np.zeros(0, np.int64)])
            self._idx[key] = idx
        return idx

    def replay(self, src_codes):
        """Return (final uint16[N], tag uint8[N], op_index int32[N]) for the current log."""
        final = src_codes["bill_eugene_code"].astype(np.uint16).copy()
        tag = np.zeros(self.n, np.uint8)
        opi = np.full(self.n, -1, np.int32)
        for k, o in enumerate(self.active_ops()):
            idx = self.op_cells(o)
            if idx.size == 0:
                continue
            if o["op"] == "clear":                      # the section(s) go back to untouched
                final[idx] = src_codes["bill_eugene_code"][idx]
                tag[idx] = TAG_DEFAULT
                opi[idx] = -1
                continue
            if o["op"] == "base":
                vals = src_codes[o["source"]][idx]
                ok = vals != 255
                idx, vals = idx[ok], vals[ok]
                final[idx] = vals
                tag[idx] = BASE_TAGS[o["source"]]
            else:
                final[idx] = o["to"]
                tag[idx] = TAG_MANUAL
            opi[idx] = k
        return final, tag, opi

    def add_op(self, op):
        with self.lock:
            op = dict(op, id=uuid.uuid4().hex[:10], reviewer=self.reviewer, ts=now())
            self._append_op(op)
            return op

    def undo(self, target_id=None):
        """Undo the last edit, or a named one (later edits keep the cells they recorded)."""
        with self.lock:
            act = self.active_ops()
            if target_id:
                act = [o for o in act if o["id"] == target_id]
            if not act:
                return None
            target = act[-1]
            self._append_op({"op": "undo", "target": target["id"], "id": uuid.uuid4().hex[:10],
                             "reviewer": self.reviewer, "ts": now()})
            return target

    def archive(self, archive_root: Path):
        """Move this session's files aside (reset). Returns the archive folder."""
        with self.lock:
            dest = archive_root / f"{self.dir.name}_{time.strftime('%Y%m%d_%H%M%S')}"
            archive_root.mkdir(parents=True, exist_ok=True)
            self.dir.rename(dest)
            return dest

    # ---------- comments / annotations
    def upsert(self, kind, item):
        items = self.comments if kind == "comments" else self.annotations
        with self.lock:
            if item.get("id"):
                for i, it in enumerate(items):
                    if it["id"] == item["id"]:
                        items[i] = dict(it, **item, updated=now())
                        break
                else:
                    raise KeyError(item["id"])
            else:
                item = dict(item, id=uuid.uuid4().hex[:10], reviewer=self.reviewer, ts=now())
                items.append(item)
            self._write(f"{kind}.json", items)
            return item

    def delete(self, kind, item_id):
        items = self.comments if kind == "comments" else self.annotations
        with self.lock:
            items[:] = [it for it in items if it["id"] != item_id]
            self._write(f"{kind}.json", items)
