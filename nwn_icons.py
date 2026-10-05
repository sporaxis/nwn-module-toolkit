"""
nwn_icons.py - an item's inventory icon, as the game builds it.

baseitems.2da says, per base item, how its picture is made (ModelType) and the icon name stem (ItemClass):
  0 / 1  simple item       i<itemclass>_<nnn>.tga            nnn = ModelPart1   (potions, rings, helmets, cloaks...)
  2      composite weapon  i<itemclass>_b_<nnn>, _m_, _t_    bottom / middle / top = ModelPart1 / 2 / 3, stacked
  3      armour            the game assembles armour from body parts - shown with its DefaultIcon
anything else, or no picture found: DefaultIcon from baseitems.2da.
Pictures are looked up in the game's order: haks (first listed wins), the module, the override, then the base game
(the NWN install folder from Settings). TGA pictures are shown; DDS-only pictures are reported (no preview).

Used by the dashboard (one IconResolver per analysis, see nwn_dashboard.icon_resolver) to show item pictures.

Reads: the analysis index (index.sqlite, opened read-only), the module / hak / override files it lists, and the
base game's archives. Writes nothing: pictures are returned as PNG data URLs, built in memory.

Limits: armour (ModelType 3) is not assembled from its parts - its DefaultIcon is shown. When several sources hold a
blueprint of the same name, its parts come from the copy the game loads (first-listed hak, the module, the override:
the same order as the pictures), so the picture matches what players see.

Thread safety: one resolver may be shared by request threads; every public method takes the resolver's own lock,
so the open archive handles (one seek-and-read at a time) and the caches are never used by two threads at once.
"""
from __future__ import annotations

import base64
import os
import threading

import nwnlib as n


class IconResolver:
    """Finds an item's inventory picture(s) for the dashboard. Read-only; results are cached per resource name.

    Holds open archive handles: call close() when done."""

    def __init__(self, analysis_dir, nwn_root=None):
        """analysis_dir: analysis folder with index.sqlite. nwn_root: game install folder (default: the one stored
        in the index's meta table); without it base-game pictures and baseitems.2da can't be found."""
        self.dir = analysis_dir
        # read-only open (nwnlib.sqlite_ro); check_same_thread=False because the dashboard reuses one resolver
        # across its request threads
        self.db = n.sqlite_ro(os.path.join(analysis_dir, "index.sqlite"), check_same_thread=False)
        meta = dict(self.db.execute("SELECT key, value FROM meta").fetchall())
        self.nwn_root = nwn_root or meta.get("nwn_root") or None
        rows = self.db.execute("SELECT id, path, kind, priority FROM sources").fetchall()
        # search order = game precedence (nwnlib.source_rank): haks in module.ifo order, the module, the override
        self.sources = sorted(rows, key=lambda r: (n.source_rank(r[2], r[3]), r[0]))
        self._erfs = {}          # source id -> open nwnlib.Erf (opened on first use)
        self._base = None        # nwnlib.BaseGame index of the install (built on first use)
        self._cache = {}         # "name.ext" -> bytes or None (None = not found anywhere)
        self._baseitems = None   # baseitems.2da as {row number (text): {column: value}}
        # source id -> its place in game order, to pick the winning copy of a blueprint (see item_fields)
        self._rank = {r[0]: i for i, r in enumerate(self.sources)}
        # re-entrant: icon() calls item_fields(), baseitems() and get(), which all take it too
        self._lock = threading.RLock()

    def close(self):
        """Close the open archives and the database connection."""
        for e in self._erfs.values():
            e.close()
        self.db.close()

    # -- resources in game order
    def get(self, name):
        """Bytes of resource name ("x.tga"), from the winning source, else the base game; None if not found.

        Only sources the index lists as having the file are opened. An archive that can't be read now (moved,
        damaged) is skipped. The answer, found or not, is cached for the life of the resolver."""
        with self._lock:
            return self._get(name.lower())

    def _get(self, name):
        """get() without the lock (name already lower case)."""
        if name in self._cache:
            return self._cache[name]
        data = None
        hits = {sid for (sid,) in self.db.execute("SELECT source_id FROM files WHERE lower(relpath)=?", (name,))}
        for sid, path, kind, _prio in self.sources:
            if sid not in hits:
                continue
            if os.path.isdir(path):
                try:                              # a folder source: the file may have gone or be unreadable now
                    data = n.read_file_inside(path, name)
                except (OSError, ValueError):
                    data = None
            else:
                try:
                    erf = self._erfs.get(sid) or self._erfs.setdefault(sid, n.Erf(path))
                    ent = next((e for e in erf.entries if e.filename == name), None)
                    data = erf.read(ent) if ent else None
                except (OSError, ValueError):
                    data = None
            if data is not None:
                break
        if data is None and self.nwn_root:
            try:
                self._base = self._base or n.BaseGame(self.nwn_root)
                data = self._base.get(name)
            except Exception:  # noqa - no game data: treated as not found
                data = None
        self._cache[name] = data
        return data

    def baseitems(self):
        """baseitems.2da (the winning copy) as {row number as text: {column: value}}; {} if not found."""
        with self._lock:
            if self._baseitems is None:
                self._baseitems = {}
                raw = self.get("baseitems.2da")
                if raw:
                    import nwn_2da
                    table, _ = nwn_2da.parse(n.decode_text(raw))
                    cols = table["columns"]
                    # keyed by the line's position, which is the BaseItem number the game uses (the first-column
                    # label is for people only and can drift in hand-merged tables)
                    for pos, r in enumerate(table["rows"]):
                        self._baseitems[str(pos)] = {c: r[i + 1] for i, c in enumerate(cols)}
            return self._baseitems

    def item_fields(self, node):
        """(BaseItem, ModelPart1..3) of a blueprint (bp:x.uti) or a placed/carried item (inst:...).

        Returns {label: value text} read from the index's fields table (only the labels present), or None when the
        index has no such item. A blueprint held by several sources is read from the copy the game loads (the
        earliest in self.sources: first-listed hak, ..., the module, the override)."""
        with self._lock:
            return self._item_fields(node)

    def _item_fields(self, node):
        """item_fields() without the lock."""
        want = ("BaseItem", "ModelPart1", "ModelPart2", "ModelPart3", "xModelPart1")
        if node.startswith("bp:"):
            copies = self.db.execute("SELECT id, source_id FROM files WHERE node=?", (node,)).fetchall()
            if not copies:
                return None
            # the winning copy = the one from the source the game reads first; ties (same source) keep the lowest id
            row = min(copies, key=lambda c: (self._rank.get(c[1], len(self._rank)), c[0]))
            # path=label: a top-level field of the blueprint, not one inside a nested struct
            vals = {lab: v for lab, v in self.db.execute(
                "SELECT label, value FROM fields WHERE file_id=? AND path=label AND label IN (?,?,?,?,?)", (row[0], *want))}
        else:
            row = self.db.execute("SELECT file_id, path FROM objects WHERE node=?", (node,)).fetchone()
            if not row:
                return None
            fid, path = row
            vals = {}
            for p, lab, v in self.db.execute("SELECT path, label, value FROM fields WHERE file_id=? AND path LIKE ? "
                                             "AND label IN (?,?,?,?,?)", (fid, path + "/%", *want)):
                if p.count("/") == path.count("/") + 1:     # a direct field of this item, not of an item inside it
                    vals[lab] = v
        return vals

    def _picture(self, stem):
        """dict(name, png data URL or None, note) for picture stem (TGA preferred over DDS); None if neither
        exists."""
        for ext in ("tga", "dds"):
            data = self.get(f"{stem}.{ext}")
            if data:
                if ext == "tga":
                    png = n.tga_to_png(data, 128)
                    if png:
                        return dict(name=f"{stem}.tga", png="data:image/png;base64," + base64.b64encode(png).decode())
                return dict(name=f"{stem}.{ext}", png=None, note="DDS picture - no preview")
        return None

    def icon(self, node):
        """The inventory picture of item node (bp:x.uti or inst:...), as the game would build it.

        Returns dict(node, found, base_item, item_class, model_type, parts, layers, missing, note): layers are the
        pictures to draw on top of each other (bottom first), missing the picture names looked for and not found.
        When the item or baseitems.2da is unknown: dict(node, found=False, note[, base_item])."""
        with self._lock:
            return self._icon(node)

    def _icon(self, node):
        """icon() without the lock."""
        vals = self.item_fields(node)
        if vals is None:
            return dict(node=node, found=False, note="not an item the index knows")
        bi = str(vals.get("BaseItem") or "")
        row = self.baseitems().get(bi)
        if not row:
            return dict(node=node, found=False, base_item=bi,
                        note="baseitems.2da not found (set the NWN install folder in Settings)" if not self.baseitems()
                        else f"base item {bi} is not in baseitems.2da")
        cls = (row.get("ItemClass") or "").lower()
        mtype = str(row.get("ModelType") or "0")

        def part(k):
            """ModelPart<n> as a number; 0 when the field is missing or not a number (a hand-edited or odd
            blueprint must not stop the whole icon batch)."""
            try:
                return int(vals.get(k) or 0)
            except (TypeError, ValueError):
                return 0
        layers, missing = [], []
        if mtype == "2" and cls:
            # composite (weapons): three stacked pictures i<class>_b_<nnn>, _m_, _t_ (see the module docstring)
            for letter, k in (("b", "ModelPart1"), ("m", "ModelPart2"), ("t", "ModelPart3")):
                stem = f"i{cls}_{letter}_{part(k):03d}"
                pic = self._picture(stem)
                (layers.append(pic) if pic else missing.append(stem + ".tga"))
        elif mtype in ("0", "1") and cls:
            stem = f"i{cls}_{part('ModelPart1'):03d}"
            pic = self._picture(stem)
            (layers.append(pic) if pic else missing.append(stem + ".tga"))
        note = ""
        if not layers:
            d = (row.get("DefaultIcon") or "").lower()
            pic = self._picture(d) if d else None
            if pic:
                layers.append(pic)
                note = "armour is assembled from body parts in game - this is its default icon" if mtype == "3" else \
                    "its own picture was not found - this is the base item's default icon"
        return dict(node=node, found=bool(layers), base_item=bi, item_class=cls, model_type=mtype,
                    parts=[part("ModelPart1"), part("ModelPart2"), part("ModelPart3")],
                    layers=layers, missing=missing, note=note)
