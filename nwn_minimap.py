"""
nwn_minimap.py - area minimaps, a map of the areas that join edge to edge, and checks on every area transition.

Why it exists
-------------
The toolset shows one area's minimap at a time and nothing shows how a module's areas join up. This module draws each
area's minimap the way the game does (from the tileset's per-tile minimap images), lays out areas that join edge to
edge (a road leaving the east edge of one area and arriving at the west edge of the next) as one joined map, and checks
every area transition (door or trigger LinkedTo) for the mistakes that strand or confuse players. It also reports
minimap images that exist nowhere (blank squares in game - an error, the owner's call) and areas whose Environment
setting says Underground although everything else says outdoors.

Two entry points for the toolkit
--------------------------------
- map_checks(db, nwn_root, label): called by nwn_analysis during an analysis. Names and positions only (no image is
  decoded), so it is quick (a 522-area world: about 8 s). Returns Issues-page issues (categories transition_<check>,
  minimap_missing, area_tileset_missing, area_tile_unknown / _model_missing / _empty, area_underground_outdoor,
  area_above_ground_cave) and a summary.
- build_maps(analysis) / export_png(...): the dashboard's Map page. Draws every area and every joined map once, into
  <analysis>/maps (map.json + one PNG per map), the first time the page is opened (the same 522-area world: about
  30 s); export_png draws one map or area at 16 px per tile when asked.

What it reads / writes
----------------------
Reads the analysis index (areas' tile lists and settings, the doors, triggers and waypoints of every .git, script
sources) and, through Resources, the copies of tilesets (.set) and minimap images (.tga / .dds) the game uses: haks in
module.ifo order, the module, the base game. The builder's own override folder is left out (players don't have it).
Writes only <analysis>/maps (build_maps, via a temporary folder renamed into place). Never writes the module, haks or
game. Standard library only.

How the game builds a minimap (what this copies)
------------------------------------------------
An area is Width x Height tiles of 10 m. Tile_List is stored row by row from the south-west corner (index = y * Width
+ x, y growing north). Each tile names a row of the tileset (.set [TILE<Tile_ID>]); that row's ImageMap2D is a small
square image drawn into the tile's cell, turned by Tile_Orientation x 90 degrees counter-clockwise (checked on a real
module: walls join up this way and not the other).

Limits (said where relevant)
----------------------------
- A hole (a tile number the tileset doesn't have, or a tile position the tile list doesn't fill) is drawn dark red; the
  rest of the area still draws, tile by tile. A tile whose model is missing still shows its minimap image (the Issues
  page reports it).
- A tile whose tileset (.set) or minimap image was not found is drawn light grey (never black: black is what the game
  draws for rock and dense forest).
- The layout trusts transition positions. A transition that leaves within EDGE_M of an edge and arrives within ARRIVE_M
  of the opposite edge joins two areas; one with only one end at an edge places the other area beyond that edge after
  all true edge links; anything else is a door or portal and does not join. Links that don't fit a flat grid are drawn
  overlapping and reported (map_does_not_close, Map page only).
- Tile heights (raised tiles) are not drawn.
"""
from __future__ import annotations

import json
import math
import os
import re
import struct
import zlib
from collections import defaultdict, deque

TILE_M = 10.0                 # one tile is 10 x 10 metres
EDGE_M = 12.0                 # a transition within this many metres of an area's border counts as "at that edge"
ARRIVE_M = 40.0               # the arrival end may sit this far in from the opposite border (builders often put the
                              # landing waypoint a few tiles inside a big area) and still count as an edge link
GREY = (90, 90, 90)
MISSING_FILL = (150, 150, 150)     # a tile with no minimap image (render_area)
MISSING_EDGE = (110, 110, 110)
HOLE_FILL = (170, 45, 45)          # a hole: a tile the tileset doesn't have, or a tile position with no tile
HOLE_EDGE = (110, 25, 25)
SIDES = ("north", "east", "south", "west")
OPPOSITE = {"north": "south", "south": "north", "east": "west", "west": "east"}


# ============================================================================================ textures
def decode_tga(data: bytes):
    """(width, height, rgba bytes, rows top to bottom) of an uncompressed or RLE truecolour/greyscale TGA, or None.
    Same header rules as nwnlib.tga_to_png (18-byte header; 2/3 raw, 10/11 RLE; descriptor bit 5 = top-down rows)."""
    if len(data) < 18:
        return None
    id_len, cmap_type, img_type = data[0], data[1], data[2]
    cmap_len, cmap_bits = struct.unpack_from("<H", data, 5)[0], data[7]
    w, h = struct.unpack_from("<HH", data, 12)
    bpp, desc = data[16], data[17]
    px = bpp // 8
    if img_type not in (2, 3, 10, 11) or not w or not h or w * h > 2048 * 2048 or px not in (1, 3, 4):
        return None
    pos = 18 + id_len + (cmap_len * ((cmap_bits + 7) // 8) if cmap_type else 0)
    need = w * h * px
    if img_type in (2, 3):
        raw = data[pos:pos + need]
    else:
        out = bytearray()
        while len(out) < need and pos < len(data):
            hdr = data[pos]; pos += 1
            cnt = (hdr & 0x7F) + 1
            if hdr & 0x80:
                out += data[pos:pos + px] * cnt; pos += px
            else:
                out += data[pos:pos + cnt * px]; pos += cnt * px
        raw = bytes(out)
    if len(raw) < need:
        return None
    rgba = bytearray(w * h * 4)
    top_down = bool(desc & 0x20)
    for y in range(h):
        sy = y if top_down else h - 1 - y
        for x in range(w):
            i = (sy * w + x) * px
            o = (y * w + x) * 4
            if px == 1:
                rgba[o:o + 4] = bytes((raw[i], raw[i], raw[i], 255))
            elif px == 3:
                rgba[o:o + 4] = bytes((raw[i + 2], raw[i + 1], raw[i], 255))
            else:
                rgba[o:o + 4] = bytes((raw[i + 2], raw[i + 1], raw[i], raw[i + 3]))
    return w, h, bytes(rgba)


def _c565(c):
    """RGB from a 16-bit 5:6:5 colour."""
    r, g, b = (c >> 11) & 31, (c >> 5) & 63, c & 31
    return (r << 3 | r >> 2, g << 2 | g >> 4, b << 3 | b >> 2)


def _dxt(data, off, w, h, alpha):
    """Decode DXT1 (alpha False) or DXT5 (alpha True) blocks starting at off into RGBA bytes, rows top to bottom."""
    rgba = bytearray(w * h * 4)
    bs = 16 if alpha else 8
    bw, bh = max(1, (w + 3) // 4), max(1, (h + 3) // 4)
    if off + bw * bh * bs > len(data):
        return None
    for by in range(bh):
        for bx in range(bw):
            p = off + (by * bw + bx) * bs
            amap = None
            if alpha:                                   # DXT5: two alpha endpoints + 16 x 3-bit indices
                a0, a1 = data[p], data[p + 1]
                bits = int.from_bytes(data[p + 2:p + 8], "little")
                if a0 > a1:
                    pal = [a0, a1] + [((6 - i) * a0 + (i + 1) * a1) // 7 for i in range(6)]
                else:
                    pal = [a0, a1] + [((4 - i) * a0 + (i + 1) * a1) // 5 for i in range(4)] + [0, 255]
                amap = [pal[(bits >> (3 * i)) & 7] for i in range(16)]
                p += 8
            c0, c1 = struct.unpack_from("<HH", data, p)
            k0, k1 = _c565(c0), _c565(c1)
            if c0 > c1 or alpha:
                cols = [k0, k1, tuple((2 * a + b) // 3 for a, b in zip(k0, k1)), tuple((a + 2 * b) // 3 for a, b in zip(k0, k1))]
                trans = False
            else:
                cols = [k0, k1, tuple((a + b) // 2 for a, b in zip(k0, k1)), (0, 0, 0)]
                trans = True
            idx = struct.unpack_from("<I", data, p + 4)[0]
            for i in range(16):
                x, y = bx * 4 + (i & 3), by * 4 + (i >> 2)
                if x >= w or y >= h:
                    continue
                ci = (idx >> (2 * i)) & 3
                r, g, b = cols[ci]
                a = amap[i] if amap else (0 if trans and ci == 3 else 255)
                o = (y * w + x) * 4
                rgba[o:o + 4] = bytes((r, g, b, a))
    return bytes(rgba)


def decode_dds(data: bytes):
    """(width, height, rgba) of the first mip level of a DDS texture, or None.

    Two layouts occur in NWN content: the standard Microsoft DDS ("DDS " + 124-byte header; DXT1, DXT5 or plain
    32/24-bit RGB(A)), and BioWare's own (20-byte header: width, height, channels (3 = DXT1, 4 = DXT5), base-level
    size, alpha mean (float) - as neverwinter.nim reads it). Both store blocks top row first."""
    if data[:4] == b"DDS " and len(data) >= 128:
        h, w = struct.unpack_from("<II", data, 12)
        flags4, fourcc, bits = struct.unpack_from("<I4sI", data, 80)
        if w * h > 2048 * 2048 or not w or not h:
            return None
        if fourcc == b"DXT1":
            px = _dxt(data, 128, w, h, False)
        elif fourcc in (b"DXT5", b"DXT3"):
            px = _dxt(data, 128, w, h, True) if fourcc == b"DXT5" else None
        elif not (flags4 & 4) and bits in (24, 32):
            step = bits // 8
            if 128 + w * h * step > len(data):
                return None
            rgba = bytearray()
            for i in range(w * h):
                q = 128 + i * step
                rgba += bytes((data[q + 2], data[q + 1], data[q], data[q + 3] if step == 4 else 255))
            px = bytes(rgba)
        else:
            px = None
        return (w, h, px) if px else None
    if len(data) >= 20:
        w, h, ch = struct.unpack_from("<III", data, 0)
        if 0 < w <= 2048 and 0 < h <= 2048 and ch in (3, 4):
            px = _dxt(data, 20, w, h, ch == 4)
            return (w, h, px) if px else None
    return None


def decode_texture(data: bytes, ext: str):
    """decode_tga or decode_dds by extension; None when it can't be read."""
    try:
        return decode_tga(data) if ext == "tga" else decode_dds(data)
    except (struct.error, IndexError, ValueError):
        return None


# ============================================================================================ images
class Canvas:
    """An RGB image in memory: width, height, bytearray of rows top to bottom."""
    def __init__(self, w, h, bg=(0, 0, 0)):
        self.w, self.h = w, h
        self.px = bytearray(bytes(bg) * (w * h))

    def put_tile(self, x0, y0, size, tex, ori, _cache={}):
        """Draw a decoded texture (w, h, rgba) into the size x size cell at (x0, y0), turned ori x 90 degrees
        counter-clockwise, scaled to the cell (nearest pixel), blended over grey where it is see-through. The scaled
        and turned cell is worked out once per (texture, turn, size) and kept (_cache), then copied row by row."""
        # keyed by the texture object's id; the object itself is kept beside the rows and compared, because an id is
        # reused once its object is freed (a later map would otherwise get another texture's picture)
        key = (id(tex), ori % 4, size)
        hit = _cache.get(key)
        rows = hit[1] if hit is not None and hit[0] is tex else None
        if rows is None:
            tw, th, rgba = tex
            rows = []
            for dy in range(size):
                row = bytearray()
                for dx in range(size):
                    u, v = dx, dy
                    for _ in range(ori % 4):          # undo one CCW turn: (u, v) <- (size-1-v, u)
                        u, v = size - 1 - v, u
                    o = ((v * th // size) * tw + u * tw // size) * 4
                    a = rgba[o + 3]
                    row += bytes(rgba[o + c] if a == 255 else (rgba[o + c] * a + GREY[c] * (255 - a)) // 255
                                 for c in range(3))
                rows.append(bytes(row))
            if len(_cache) > 20000:
                _cache.clear()
            _cache[key] = (tex, rows)
        for dy, row in enumerate(rows):
            Y = y0 + dy
            if 0 <= Y < self.h and 0 <= x0 and x0 + size <= self.w:
                q = (Y * self.w + x0) * 3
                self.px[q:q + size * 3] = row

    def fill(self, x0, y0, w, h, col):
        """A filled rectangle (clipped)."""
        for y in range(max(0, y0), min(self.h, y0 + h)):
            q = (y * self.w + max(0, x0)) * 3
            n = max(0, min(self.w, x0 + w) - max(0, x0))
            self.px[q:q + 3 * n] = bytes(col) * n

    def png(self) -> bytes:
        """The image as PNG bytes (8-bit RGB, no interlace)."""
        raw = bytearray()
        for y in range(self.h):
            raw += b"\0" + self.px[y * self.w * 3:(y + 1) * self.w * 3]

        def chunk(t, d):
            c = struct.pack(">I", len(d)) + t + d
            return c + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
        return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", self.w, self.h, 8, 2, 0, 0, 0)) +
                chunk(b"IDAT", zlib.compress(bytes(raw), 6)) + chunk(b"IEND", b""))


def parse_set(text: str):
    """{tile id: dict(model, imagemap2d)} from a tileset .set file (INI text). Names lower case."""
    tiles, cur = {}, None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            sec = line[1:-1].strip().lower()
            cur = int(sec[4:]) if sec.startswith("tile") and sec[4:].isdigit() else None
            if cur is not None:
                tiles[cur] = {}
            continue
        if cur is not None and "=" in line:
            k, _, v = line.partition("=")
            k = k.strip().lower()
            if k in ("model", "imagemap2d"):
                tiles[cur][k] = v.strip().lower()
    return tiles


def render_area(area, set_tiles, texture, px=16):
    """(Canvas, stats) for one area's minimap.

    area: dict(Width, Height, tiles=[[Tile_ID, Tile_Orientation, Tile_Height], ...] in .are order); set_tiles: the
    tileset's parse_set() result or None; texture(name) -> decoded texture or None; px: pixels per tile.
    stats: dict(tiles, drawn, no_set, no_texture, holes) - how many cells could not be drawn and why. Each cell is drawn
    on its own, so a hole (a tile number the tileset doesn't have, or a position the tile list doesn't fill) is drawn
    dark red and the rest of the area still draws."""
    W, H = int(area.get("Width") or 0), int(area.get("Height") or 0)
    cv = Canvas(max(1, W * px), max(1, H * px), GREY)
    st = dict(tiles=W * H, drawn=0, no_set=0, no_texture=0, holes=0)
    tiles = area.get("tiles", [])[:W * H]

    def hole(X, Y):
        st["holes"] += 1
        cv.fill(X, Y, px, px, HOLE_EDGE)
        if px > 2:
            cv.fill(X + 1, Y + 1, px - 2, px - 2, HOLE_FILL)
    for i in range(len(tiles), W * H):               # positions the tile list doesn't reach (tile_holes: "empty")
        hole((i % W) * px, (H - 1 - i // W) * px)
    for i, t in enumerate(tiles):
        tid, ori = t[0], t[1]
        x, y = i % W, i // W
        X, Y = x * px, (H - 1 - y) * px               # tile rows run south to north; images run top (north) down
        if set_tiles is not None and tid not in set_tiles:
            hole(X, Y)                                # the tileset has no [TILE<id>] section (tile_holes: "unknown")
            continue
        name = (set_tiles or {}).get(tid, {}).get("imagemap2d") if set_tiles else None
        tex = texture(name) if name else None
        if tex:
            cv.put_tile(X, Y, px, tex, ori)
            st["drawn"] += 1
        else:
            st["no_set" if set_tiles is None else "no_texture"] += 1
            if name:
                st.setdefault("missing", {})[name] = st.get("missing", {}).get(name, 0) + 1
            # a tile whose minimap image can't be found: plain light grey with a darker edge, so it reads as
            # "no image" and is never mistaken for a tile the game really draws black (solid rock, dense forest)
            cv.fill(X, Y, px, px, MISSING_EDGE)
            if px > 2:
                cv.fill(X + 1, Y + 1, px - 2, px - 2, MISSING_FILL)
    return cv, st


# ============================================================================================ transitions
def _centre(o):
    """A transition's position in its area (metres): a trigger's polygon centre (its points are relative to its
    position), else the object's own position."""
    g = o.get("geom")
    if g:
        return (o["x"] + sum(p[0] for p in g) / len(g), o["y"] + sum(p[1] for p in g) / len(g))
    return o["x"], o["y"]


def _inside(pt, o):
    """Is point pt inside trigger o's polygon (ray casting)?"""
    g = o.get("geom") or []
    if len(g) < 3:
        return False
    x, y = pt
    poly = [(o["x"] + p[0], o["y"] + p[1]) for p in g]
    hit = False
    for (x1, y1), (x2, y2) in zip(poly, poly[1:] + poly[:1]):
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / ((y2 - y1) or 1e-9) + x1:
            hit = not hit
    return hit


def edge_of(area, pt, margin=EDGE_M):
    """Which border of the area a point is at ("north", "east", "south", "west") or None when it is further than
    margin metres from every border. Areas run from (0, 0) at the south-west corner."""
    W, H = int(area.get("Width") or 0) * TILE_M, int(area.get("Height") or 0) * TILE_M
    x, y = pt
    d = {"west": x, "east": W - x, "south": y, "north": H - y}
    side = min(d, key=d.get)
    return side if d[side] <= margin else None


def transitions(areas, objects):
    """Every door / trigger with a LinkedTo, resolved and checked. Returns (links, findings).

    areas: {resref: area dict}; objects: [dict(area, kind, path, tag, x, y, LinkedTo?, LinkedToFlags?, Type?, geom?)].
    links: [dict(src, dst, src_area, dst_area, kind ("edge" | "portal"), side, dst_side)] for transitions that land.
    findings: [dict(severity, check, area, object, detail, fix)] - see TRANSITION_CHECKS."""
    by_tag = defaultdict(list)
    by_tag_ci = defaultdict(list)
    for o in objects:
        if o["kind"] in ("Door List", "WaypointList") and o["tag"]:
            by_tag[o["tag"]].append(o)
            by_tag_ci[o["tag"].lower()].append(o)
    links, findings = [], []

    def lab(o):
        return f"{o['kind'].replace(' List', '').replace('List', '').lower()} {o['tag'] or o['path']}"

    def find(sev, check, o, detail, fix):
        findings.append(dict(severity=sev, check=check, area=o["area"], object=lab(o), path=o["path"], detail=detail, fix=fix))
    srcs = [o for o in objects if o.get("LinkedTo") and o["kind"] in ("Door List", "TriggerList")]
    trig_by_area = defaultdict(list)          # transition triggers per area, for the "lands inside one" test
    for t in srcs:
        if t["kind"] == "TriggerList":
            trig_by_area[t["area"]].append(t)
    nm = lambda r: (areas.get(r) or {}).get("Name") or r      # noqa: E731 - area name for the messages
    for o in srcs:
        a = areas.get(o["area"])
        if a is None:
            continue
        to = o["LinkedTo"]
        flag = str(o.get("LinkedToFlags") or "0")
        dests = by_tag.get(to, [])
        if o["kind"] == "TriggerList" and str(o.get("Type") or "0") != "1":
            find("warning", "not_a_transition", o, f"has LinkedTo '{to}' but its type is not Area Transition, so "
                 "clicking it does nothing", "Set the trigger's type to Area Transition, or clear LinkedTo.")
        if flag == "0":
            find("warning", "no_link_flag", o, f"LinkedTo '{to}' is set but 'Link to' is None (LinkedToFlags 0) - the "
                 "transition is off", "In the toolset pick the destination again (Area Transition tab).")
        if not dests:
            ci = by_tag_ci.get(to.lower(), [])
            if ci:
                find("error", "dest_case", o, f"destination tag '{to}' not found; '{ci[0]['tag']}' in "
                     f"{nm(ci[0]['area'])} differs only in capitals (tags are case-sensitive)",
                     "Set LinkedTo to the exact tag, or rename the destination's tag.")
            elif o.get("OnClick"):
                # a script on the click may move the player itself (a "scripted transition"): worth a look, no more
                find("info", "dest_missing_scripted", o, f"destination '{to}' does not exist, but its OnClick script "
                     f"{o['OnClick']} may move the player itself", "Check the script; clear LinkedTo if the script does the jump.")
            else:
                find("error", "dest_missing", o, f"destination '{to}' does not exist: no door or waypoint has that tag",
                     "Pick an existing destination in the toolset (Area Transition tab), or place a waypoint with that tag.")
            continue
        want = "Door List" if flag == "1" else "WaypointList" if flag == "2" else None
        typed = [d for d in dests if want is None or d["kind"] == want] or dests
        if want and not [d for d in dests if d["kind"] == want]:
            find("warning", "dest_type", o, f"'Link to' says {'door' if flag == '1' else 'waypoint'} but '{to}' is a "
                 f"{'door' if typed[0]['kind'] == 'Door List' else 'waypoint'}", "Pick the destination again in the toolset.")
        d = typed[0]
        if len({x["area"] for x in typed}) > 1 or len(typed) > 1:
            find("warning", "dest_ambiguous", o, f"{len(typed)} objects have tag '{to}' (in "
                 f"{', '.join(sorted({nm(x['area']) for x in typed})[:4])}) - the game picks one of them",
                 "Give the destination a unique tag and point LinkedTo at it.")
        b = areas.get(d["area"])
        if b is None:
            find("error", "dest_area_missing", o, f"destination '{to}' is in area '{d['area']}', which is not in the module",
                 "Restore the area or pick another destination.")
            continue
        p_src, p_dst = _centre(o), (d["x"], d["y"])
        Wb, Hb = int(b.get("Width") or 0) * TILE_M, int(b.get("Height") or 0) * TILE_M
        if not (0 <= p_dst[0] <= Wb and 0 <= p_dst[1] <= Hb):
            find("error", "dest_outside", o, f"destination '{to}' at ({p_dst[0]:.0f}, {p_dst[1]:.0f}) is outside "
                 f"{nm(d['area'])} ({Wb:.0f} x {Hb:.0f} m)", "Move the destination inside the area.")
        # landing inside a transition trigger of the destination area: the player is sent straight on (or back)
        # (inside the trigger that leads straight back is common and harmless: a trigger fires when walked into, so
        # the player only has to step off it - left out; inside one that leads somewhere else is worth a look)
        for t in trig_by_area.get(d["area"], ()):
            if t is not o and _inside(p_dst, t):
                back_to = [x for x in by_tag.get(t["LinkedTo"], []) if x["area"] == o["area"]]
                if not back_to:
                    find("warning", "lands_in_transition", o, f"arrival point '{to}' in {nm(d['area'])} is inside transition "
                         f"trigger {t['tag'] or t['path']}, which leads to '{t['LinkedTo']}' elsewhere - walking off it "
                         "sends the player on", "Move the arrival waypoint off the trigger.")
                break
        s1 = edge_of(a, p_src)
        s2 = edge_of(b, p_dst, ARRIVE_M)
        kind = "edge" if s1 and s2 and s2 == OPPOSITE[s1] and o["area"] != d["area"] else "portal"
        if s1 and s2 and s2 != OPPOSITE[s1] and o["kind"] == "TriggerList" and o["area"] != d["area"]:
            find("info", "edge_mismatch", o, f"leaves {nm(o['area'])} at its {s1} edge but arrives at the {s2} edge of "
                 f"{nm(d['area'])} (expected {OPPOSITE[s1]})", "Fine if intended; for a seamless map put the arrival on the "
                 "opposite edge.")
        half = None
        if kind == "portal" and o["area"] != d["area"]:
            # one end at a border, the other inside its area (a river crossing, a gate in the middle of a town):
            # the inside area is drawn beyond that border, but only if no true edge link places it first (layout)
            if s1 and not edge_of(b, p_dst, ARRIVE_M):
                half = (o["area"], s1, p_src, d["area"], p_dst)
            elif not s1 and edge_of(b, p_dst):
                half = (d["area"], edge_of(b, p_dst), p_dst, o["area"], p_src)
        links.append(dict(src=o, dst=d, src_area=o["area"], dst_area=d["area"], kind=kind, side=s1, dst_side=s2,
                          p_src=p_src, p_dst=p_dst, half=half))
    # round trips: does a transition come back, and close to where the player left?
    back = defaultdict(list)
    for L in links:
        back[(L["dst_area"], L["src_area"])].append(L)
    for L in links:
        if L["src_area"] == L["dst_area"]:
            continue
        ret = back.get((L["src_area"], L["dst_area"]), [])
        if not ret:
            find("info", "one_way", L["src"], f"goes to {nm(L['dst_area'])} but nothing there leads back to "
                 f"{nm(L['src_area'])}", "Fine for a one-way drop; otherwise add a return transition.")
            continue
        best = min(math.dist(r["p_dst"], L["p_src"]) for r in ret)
        if best > 20:
            find("info", "return_far", L["src"], f"the way back from {nm(L['dst_area'])} lands {best:.0f} m from this "
                 "transition", "Point the return transition at a waypoint next to this one.")
    return links, findings


# ============================================================================================ layout
def layout(areas, links, keep_overlaps=True, treat_outdoor=()):
    """Stitch areas joined edge to edge into regions. Returns (regions, conflicts).

    regions: [dict(areas={resref: (x, y)} in tiles, x/y of each area's south-west corner, w, h (tiles), overlaps,
    loose)], biggest first; areas with no edge links form one-area regions. conflicts: [dict(area, other, gap, detail)]
    where two edge links disagree on where an area sits (by more than one tile) or a link puts an area on top of another.
    keep_overlaps: True (default) places an area its link puts on top of another one anyway (the world is shown whole
    even where the builder's links don't fit a flat grid), lists it in the region's "overlaps" and reports it; False
    leaves such an area to its own map.
    Areas joined only by a link with one end at a border (the other inside its area) are placed beyond that border
    after the true edge links, and listed in the region's "loose".
    treat_outdoor: resrefs to stitch as above ground whatever their Flags say (areas area_environment() found set to
    Underground by mistake), so the map shows them where players walk."""
    def env(a):
        """Underground / interior / outdoor, from the area's Flags (bit 1 underground, bit 0 interior): only areas of
        the same kind are stitched, so a cave mouth at the edge of a forest doesn't pull the cave into the forest map."""
        if a.get("resref") in treat_outdoor:
            return "outdoor"
        f = int(a.get("Flags") or 0)
        return "underground" if f & 2 else "interior" if f & 1 else "outdoor"
    adj = defaultdict(list)
    for L in links:
        if L["kind"] != "edge":
            continue
        a, b = areas[L["src_area"]], areas[L["dst_area"]]
        if env(a) != env(b):
            continue
        Wa, Ha = int(a["Width"]), int(a["Height"])
        Wb, Hb = int(b["Width"]), int(b["Height"])
        (xa, ya), (xb, yb) = L["p_src"], L["p_dst"]
        s = L["side"]
        if s == "north":
            off = (round((xa - xb) / TILE_M), Ha)
        elif s == "south":
            off = (round((xa - xb) / TILE_M), -Hb)
        elif s == "east":
            off = (Wa, round((ya - yb) / TILE_M))
        else:
            off = (-Wb, round((ya - yb) / TILE_M))
        adj[L["src_area"]].append((L["dst_area"], off))
        adj[L["dst_area"]].append((L["src_area"], (-off[0], -off[1])))
    # fallback placements from links with only one end at a border: anchor area -> [(other area, offset)]
    half_adj = defaultdict(list)
    for L in links:
        h = L.get("half")
        if not h or env(areas[h[0]]) != env(areas[h[3]]):
            continue
        anchor, side, (xb, yb), other, (xa, ya) = h
        B, A = areas[anchor], areas[other]
        Wb, Hb, Wa, Ha = int(B["Width"]), int(B["Height"]), int(A["Width"]), int(A["Height"])
        off = {"north": (round((xb - xa) / TILE_M), Hb), "south": (round((xb - xa) / TILE_M), -Ha),
               "east": (Wb, round((yb - ya) / TILE_M)), "west": (-Wa, round((yb - ya) / TILE_M))}[side]
        half_adj[anchor].append((other, off))
    pos, regions, conflicts = {}, [], []
    order = sorted(areas, key=lambda r: (-len(adj.get(r, [])), r))
    for start in order:
        if start in pos:
            continue
        reg = {start: (0, 0)}
        overlaps, loose = set(), set()
        pos[start] = (0, 0, len(regions))
        q = deque([start])
        tried_half = set()
        while True:
            if not q:
                # edge links exhausted: place areas joined only by one-border links, then carry on from them
                for a in list(reg):
                    if a in tried_half:
                        continue
                    tried_half.add(a)
                    for b, off in half_adj.get(a, []):
                        if b not in reg and b not in pos:
                            q.append(("half", a, b, off))
                if not q:
                    break
            item = q.popleft()
            is_half = isinstance(item, tuple)
            if is_half:
                _, a, b, (dx, dy) = item
                pairs = [(b, (dx, dy))]
            else:
                a = item
                pairs = adj.get(a, [])
            ax, ay = reg[a]
            for b, (dx, dy) in pairs:
                want = (ax + dx, ay + dy)
                if b in reg and is_half:
                    continue                             # an approximate placement never argues with a real one
                if b in reg:
                    gap = max(abs(reg[b][0] - want[0]), abs(reg[b][1] - want[1]))
                    if gap > 1 and not any(c["area"] in (a, b) and c["other"] in (a, b) for c in conflicts):
                        conflicts.append(dict(area=b, other=a, gap=gap, detail=f"the links between {a} and {b} disagree "
                                              f"by {gap} tiles (the map does not close here)"))
                    continue
                if b in pos:                             # already in another region (placed earlier from elsewhere)
                    continue
                wb, hb = int(areas[b]["Width"]), int(areas[b]["Height"])
                hit = next((r for r, (x, y) in reg.items() if x < want[0] + wb and want[0] < x + int(areas[r]["Width"])
                            and y < want[1] + hb and want[1] < y + int(areas[r]["Height"])), None)
                if hit:
                    # placing it here covers another area: the builder's links don't fit a flat grid at this point
                    if not any(c["area"] == b and c["other"] == hit for c in conflicts):
                        conflicts.append(dict(area=b, other=hit, gap=0, detail=f"{a}'s transition puts {b} on top of {hit}; "
                                              + ("drawn overlapping it, outlined in red" if keep_overlaps
                                                 else "drawn on its own map instead")))
                    if not keep_overlaps:
                        continue
                    overlaps.add(b)
                reg[b] = want
                pos[b] = (want[0], want[1], len(regions))
                if is_half:
                    loose.add(b)
                q.append(b)
        xs = [reg[r][0] for r in reg] + [reg[r][0] + int(areas[r]["Width"]) for r in reg]
        ys = [reg[r][1] for r in reg] + [reg[r][1] + int(areas[r]["Height"]) for r in reg]
        mx, my = min(xs), min(ys)
        reg = {r: (x - mx, y - my) for r, (x, y) in reg.items()}
        regions.append(dict(areas=reg, w=max(xs) - mx, h=max(ys) - my, overlaps=sorted(overlaps), loose=sorted(loose)))
    regions.sort(key=lambda r: -len(r["areas"]))
    return regions, conflicts


def render_region(region, areas, canvases, links, px=8):
    """Canvas of one stitched region: each area's minimap at its place (north up), a dark outline round each area,
    and a marker on each transition (green: edge link, magenta: door/portal to somewhere else). canvases: {resref:
    Canvas at px pixels per tile}."""
    W, H = region["w"] * px, region["h"] * px
    cv = Canvas(max(1, W), max(1, H), (24, 26, 30))
    for r, (x, y) in region["areas"].items():
        a, src = areas[r], canvases[r]
        X0, Y0 = x * px, H - (y + int(a["Height"])) * px
        for row in range(src.h):
            q = ((Y0 + row) * W + X0) * 3
            cv.px[q:q + src.w * 3] = src.px[row * src.w * 3:(row + 1) * src.w * 3]
        cv.fill(X0, Y0, src.w, 1, (10, 10, 10)); cv.fill(X0, Y0 + src.h - 1, src.w, 1, (10, 10, 10))
        cv.fill(X0, Y0, 1, src.h, (10, 10, 10)); cv.fill(X0 + src.w - 1, Y0, 1, src.h, (10, 10, 10))
    for L in links:
        if L["src_area"] in region["areas"]:
            x, y = region["areas"][L["src_area"]]
            ah = int(areas[L["src_area"]]["Height"])
            mx = int(x * px + L["p_src"][0] / TILE_M * px)
            my = int(H - (y * px + L["p_src"][1] / TILE_M * px))
            cv.fill(mx - 2, my - 2, 5, 5, (60, 220, 90) if L["kind"] == "edge" else (230, 60, 200))
    return cv


# ============================================================================================ area environment
OUTDOOR_WORDS = ("hill", "wood", "forest", "road", "river", "pass", "plain", "field", "coast", "beach", "valley",
                 "mountain", "moor", "swamp", "lake", "shore", "trail", "farm", "gorge", "meadow", "village", "rural")
UNDER_WORDS = ("cave", "cavern", "mine", "tunnel", "crypt", "depth", "sewer", "dungeon", "underdark", "lair", "tomb",
               "catacomb", "barrow", "beneath", "below", "cellar", "hive", "under", "grotto", "vault", "shaft")
ENV_CALL = re.compile(r"GetIsAreaAboveGround")      # the call whose answer the Underground setting decides


def _words(name, words):
    low = (name or "").lower()
    return [w for w in words if w in low]


def area_environment(areas, sets, links, env_scripts):
    """Findings for areas whose Environment setting (.are Flags bit 1: underground) disagrees with the evidence.

    areas: {resref: dict(Name, Flags, Tileset, ChanceRain, ChanceSnow, ChanceLightning, DayNightCycle, IsNight, SkyBox,
    SunShadows, vars=[[name, value]])}; sets: {tileset: dict(general={Interior: "0"/"1", ...})}; links: transitions()
    links (edge links to above-ground areas count as evidence); env_scripts: {script: [[line, text], ...]} - scripts
    that ask about an area's environment, listed as what the wrong setting changes. Returns [finding dict] as
    transitions() does (check "env_underground_outdoor" warning, "env_above_ground_cave" info). Never raises on
    missing fields; reads nothing from disk."""
    def num(a, k):
        try:
            return int(a.get(k) or 0)
        except (TypeError, ValueError):
            return 0
    above = lambda a: not num(a, "Flags") & 2
    nb = defaultdict(set)
    for L in links:
        if L["kind"] == "edge" and L["src_area"] != L["dst_area"]:
            nb[L["src_area"]].add(L["dst_area"]); nb[L["dst_area"]].add(L["src_area"])
    calls = []
    for sc, rows in sorted(env_scripts.items()):
        for line, text in rows:
            if ENV_CALL.search(text):
                calls.append(f"{sc} line {line}: {text.rstrip(';')}")
    out = []
    for r, a in sorted(areas.items()):
        f = num(a, "Flags")
        if f & 1:
            continue                                     # interiors: the above/underground choice rarely matters
        g = (sets.get((a.get("Tileset") or "").lower()) or {}).get("general", {})
        tile_out = str(g.get("Interior", "")).strip() == "0"
        tname = g.get("UnlocalizedName") or a.get("Tileset")
        weather = [f"{k[6:].lower()} {num(a, k)}%" for k in ("ChanceRain", "ChanceSnow", "ChanceLightning") if num(a, k)]
        outer = sorted(areas[x].get("Name") or x for x in nb.get(r, ()) if x in areas and above(areas[x])
                       and not num(areas[x], "Flags") & 1)
        ev_out, ev_under = [], []
        if tile_out:
            ev_out.append(f"its tileset {tname} is an exterior tileset (Interior=0 in {a.get('Tileset')}.set)")
        elif g:
            ev_under.append(f"its tileset {tname} is an interior/underground tileset")
        if weather:
            ev_out.append("weather is set (" + ", ".join(weather) + ")")
        if num(a, "DayNightCycle"):
            ev_out.append("it has a day/night cycle")
        if num(a, "SkyBox"):
            ev_out.append(f"it has a sky box (SkyBox {num(a, 'SkyBox')})")
        if outer:
            ev_out.append("it joins edge to edge with above-ground areas: " + ", ".join(outer[:4]))
        if _words(a.get("Name"), OUTDOOR_WORDS):
            ev_out.append(f"its name says {', '.join(_words(a.get('Name'), OUTDOOR_WORDS))}")
        if _words(a.get("Name"), UNDER_WORDS):
            ev_under.append(f"its name says {', '.join(_words(a.get('Name'), UNDER_WORDS))}")
        if not weather and not num(a, "DayNightCycle") and num(a, "IsNight"):
            ev_under.append("no weather and always night")
        varnote = (" Its variables: " + ", ".join(f"{n}={v}" for n, v in a.get("vars", [])[:6]) + ".") if a.get("vars") else ""
        effect = (" Scripts that read this setting: " + "; ".join(calls[:4]) + (f"; and {len(calls) - 4} more" if len(calls) > 4 else "") + ".") if calls else ""
        if f & 2 and len(ev_out) >= 3 and not ev_under:
            out.append(dict(severity="warning", check="env_underground_outdoor", area=r, object="(area settings)",
                            detail=f"{a.get('Name') or r} is set to Underground, but " + "; ".join(ev_out) + "." + varnote + effect,
                            fix="If players are meant to be outdoors here, set Area Properties > Environment to Above "
                                "ground in the toolset (the .are Flags field); scripts that check GetIsAreaAboveGround "
                                "will then treat it as the surface."))
        elif not f & 2 and len(ev_under) >= 3 and not ev_out:
            out.append(dict(severity="info", check="env_above_ground_cave", area=r, object="(area settings)",
                            detail=f"{a.get('Name') or r} is set to Above ground, but " + "; ".join(ev_under) + "." + effect,
                            fix="If this is a cave or dungeon, set Environment to Underground; otherwise nothing to do."))
    return out


def tile_holes(areas, tilesets, res):
    """{area: dict(tiles, unknown, no_model, empty)} for areas with holes - the three ways an area can have a tile
    with nothing there (a tester's report, 2026-10-27; none can be made in the toolset, only by editing files by hand
    or by using another version of a tileset than the area was built with):
      unknown   {tile id: tiles} - the area names a tile number its tileset's .set has no [TILE<id>] section for;
      no_model  {model: tiles} - the .set's tile names a model (.mdl) no hak, the module or the game has, or none
                ("(none)");
      empty     tile positions (Width x Height) the area's tile list doesn't fill.
    areas / tilesets: load_world() / load_tilesets(); an area whose tileset wasn't found is only checked for empty
    positions (area_tileset_missing covers the rest). res: Resources, for where() on models (names only, nothing is
    read). Never raises on odd data."""
    have = {}
    out = {}
    for r, a in areas.items():
        W, H = _int(a.get("Width")), _int(a.get("Height"))
        t = tilesets.get((a.get("Tileset") or "").lower())
        unknown, no_model = defaultdict(int), defaultdict(int)
        if t:
            for tid, _o, _h in a["tiles"][:W * H]:
                row = t["tiles"].get(tid)
                if row is None:
                    unknown[tid] += 1
                    continue
                model = row.get("model") or ""
                if not model:
                    no_model["(none)"] += 1
                    continue
                if model not in have:
                    have[model] = res.where(model, ("mdl",)) is not None
                if not have[model]:
                    no_model[model] += 1
        empty = max(0, W * H - len(a["tiles"]))
        if unknown or no_model or empty:
            out[r] = dict(tiles=W * H, unknown=dict(unknown), no_model=dict(no_model), empty=empty)
    return out


def _counts(d):
    """'812 (3), 813 (1)' - names or numbers with their tile counts, most first, the first eight."""
    items = sorted(d.items(), key=lambda kv: (-kv[1], str(kv[0])))
    txt = ", ".join(f"{k} ({v})" for k, v in items[:8])
    return txt + (f" and {len(items) - 8} more" if len(items) > 8 else "")


def hole_issues(areas, holes, set_source=None, hint=""):
    """Error findings (transitions() format) for tile_holes(): checks tile_unknown, tile_model_missing, tile_empty,
    one per area and kind. hint: added to tile_model_missing when a listed hak wasn't found or the base game wasn't
    read - the model may be there, so that finding is then a note. Never raises; reads nothing."""
    out = []
    for r, h in sorted(holes.items()):
        a = areas.get(r, {})
        nm, ts = a.get("Name") or r, (a.get("Tileset") or "").lower()
        where = (set_source or {}).get(ts) or "?"
        if h["unknown"]:
            k = sum(h["unknown"].values())
            out.append(dict(severity="error", check="tile_unknown", area=r, object=f"tileset {ts}",
                            detail=f"{k} of {h['tiles']} tiles in {nm} name tile numbers that tileset {ts} ({ts}.set "
                                   f"from {where}) doesn't have: {_counts(h['unknown'])}. Those tiles have nothing to "
                                   "draw or walk on: a hole in the area.",
                            fix=f"Usually the area was built with another version of the {ts} tileset: use the hak "
                                "version it was made with, or open the area in the toolset and paint those tiles again."))
        if h["no_model"]:
            k = sum(h["no_model"].values())
            only_none = set(h["no_model"]) == {"(none)"}
            out.append(dict(severity="info" if hint and not only_none else "error", check="tile_model_missing", area=r,
                            object=f"tileset {ts}",
                            detail=f"{k} of {h['tiles']} tiles in {nm} use tiles of {ts}.set (from {where}) whose model "
                                   + ("is not named in the .set" if only_none else "no hak, the module or the game has")
                                   + f": {_counts(h['no_model'])}. The game has nothing to draw there: a hole in the "
                                   "area." + ("" if only_none else hint),
                            fix=f"Add the missing tile models (.mdl, with their .wok walkmeshes) to the hak that "
                                f"provides {ts}.set ({where}) or to a hak listed above it; '(none)' means the .set's "
                                "[TILE] section has no Model line - fix the .set."))
        if h["empty"]:
            out.append(dict(severity="error", check="tile_empty", area=r, object="tile list",
                            detail=f"{h['empty']} of {h['tiles']} tile positions in {nm} hold no tile: the area is "
                                   f"{_int(a.get('Width'))} x {_int(a.get('Height'))} tiles but its tile list has "
                                   f"{h['tiles'] - h['empty']}. The toolset never does this - the area file was edited "
                                   "by hand or is damaged. The missing positions are at the north end (the list runs "
                                   "row by row from the south-west corner).",
                            fix="Restore the area file (.are) from a backup or the original download; if it was edited "
                                "by hand, add the missing Tile_List entries (one per tile, row by row from the "
                                "south-west corner)."))
    return out


def minimap_issues(areas, stats, set_source=None):
    """Error findings for tiles the in-game minimap shows blank: the tileset names a minimap image that no hak,
    the module or the base game has. One finding per area (the owner, 2026-10-10: a missing minimap image is an
    error and belongs on the Issues page).

    areas: {resref: area dict with Name, Tileset}; stats: {resref: render_area() stats, whose "missing" maps image
    name -> tiles}; set_source: {tileset: where its winning .set came from, e.g. "mysty_mines.hak"} for the detail.
    Returns [finding dict] in the transitions() format; check "minimap_missing". Never raises; reads nothing."""
    out = []
    for r, st in sorted(stats.items()):
        miss = st.get("missing") or {}
        if not miss:
            continue
        a = areas.get(r, {})
        ts = (a.get("Tileset") or "").lower()
        where = (set_source or {}).get(ts) or "the tileset"
        tiles = sum(miss.values())
        names = ", ".join(f"{k} ({v})" for k, v in sorted(miss.items(), key=lambda kv: -kv[1]))
        out.append(dict(severity="error", check="minimap_missing", area=r, object=f"tileset {ts}",
                        detail=f"{tiles} of {st.get('tiles', '?')} tiles in {a.get('Name') or r} have no minimap image, so "
                               f"they show blank on the in-game minimap. {ts}.set (from {where}) names images that no hak, "
                               f"the module or the base game contains: {names}.",
                        fix=f"Add the missing images (32x32 .tga named as listed) to {where}, or to a small hak listed "
                            "above it; the tile's walkmesh shows what each image should look like."))
    return out


# ============================================================================================ reading an analysis
# Everything below works from an analysis folder (index.sqlite + meta) so the toolkit can check and draw any module.
# Nothing here writes outside <analysis>/maps; the module, its haks and the game are only read.
MAP_VERSION = 2               # bump when map.json changes shape, so older saved maps are rebuilt (2: holes)
MAP_PX = 8                    # pixels per tile on the Map page (the owner's default, 2026-10-10)
EXPORT_PX = 16                # pixels per tile in a PNG export
AREA_KEEP = ("Name", "Tag", "Tileset", "Width", "Height", "Flags", "ChanceRain", "ChanceSnow", "ChanceLightning",
             "WindPower", "DayNightCycle", "IsNight", "SkyBox", "SunShadows", "OnEnter")
OBJ_KEEP = ("LinkedTo", "LinkedToFlags", "Type", "PointX", "PointY", "OnClick", "XPosition", "YPosition", "Tag",
            "X", "Y")
OBJ_LISTS = ("Door List", "TriggerList", "WaypointList")
TEXTURE_EXTS = ("dds", "tga")  # in one source a .dds is used before a .tga of the same name (as the game does)


def parse_set_general(text: str):
    """The [GENERAL] section of a tileset .set as {key: value} (e.g. Interior "0"/"1", UnlocalizedName). {} if none."""
    out, on = {}, False
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            on = line[1:-1].strip().lower() == "general"
            continue
        if on and "=" in line:
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip()
    return out


def load_world(db):
    """(areas, objects, module) from an analysis index (sqlite3 connection), module files only.

    areas: {resref: dict(resref, the AREA_KEEP values as text, tiles=[[Tile_ID, Tile_Orientation, Tile_Height]...]
    in .are order, vars=[[name, value], ...] from the area's .git VarTable)}.
    objects: [dict(area, kind ("Door List" | "TriggerList" | "WaypointList"), path (the GFF path, part of the node id
    inst:<area>:<path>), tag, x, y, LinkedTo?, LinkedToFlags?, Type?, OnClick?, geom=[(x, y)...] for triggers)].
    module: dict(entry_area). Reads the index's fields table only; never raises on a missing field."""
    row = db.execute("SELECT id FROM sources WHERE kind='module' LIMIT 1").fetchone()
    if not row:
        return {}, [], {}
    mod = row[0]
    are = {fid: rr.lower() for fid, rr in db.execute("SELECT id, resref FROM files WHERE ext='are' AND source_id=?", (mod,))}
    git = {fid: rr.lower() for fid, rr in db.execute("SELECT id, resref FROM files WHERE ext='git' AND source_id=?", (mod,))}
    areas = {rr: dict(resref=rr, tiles={}, vars=[]) for rr in are.values()}
    q = ",".join("?" * len(AREA_KEEP + ("Tile_ID", "Tile_Orientation", "Tile_Height")))
    for chunk in _chunks(list(are), 400):
        for fid, path, label, value in db.execute(
                f"SELECT file_id, path, label, value FROM fields WHERE file_id IN ({','.join('?' * len(chunk))}) "
                f"AND label IN ({q})", (*chunk, *AREA_KEEP, "Tile_ID", "Tile_Orientation", "Tile_Height")):
            a = areas[are[fid]]
            if path.startswith("Tile_List["):
                # Tile_List[i]/Tile_ID ... : the i-th tile, counted row by row from the south-west corner
                i = int(path[10:path.index("]")])
                k = ("Tile_ID", "Tile_Orientation", "Tile_Height").index(label) if label.startswith("Tile_") else None
                if k is not None:
                    a["tiles"].setdefault(i, [0, 0, 0])[k] = _int(value)
            elif "/" not in path and label in AREA_KEEP:
                a[label] = value
    for a in areas.values():
        a["tiles"] = [a["tiles"][i] for i in sorted(a["tiles"])]
        # an .are the indexer could not read has a files row but no fields: give it a size of 0 so drawing and layout
        # (which read Width/Height directly) treat it as an empty area instead of failing
        a["Width"], a["Height"] = str(max(0, _int(a.get("Width")))), str(max(0, _int(a.get("Height"))))
    # doors, triggers and waypoints of every .git (positions, transition settings, trigger outlines)
    by = defaultdict(dict)
    for chunk in _chunks(list(git), 400):
        for fid, path, label, value in db.execute(
                f"SELECT file_id, path, label, value FROM fields WHERE file_id IN ({','.join('?' * len(chunk))}) "
                f"AND label IN ({','.join('?' * len(OBJ_KEEP))})", (*chunk, *OBJ_KEEP)):
            top = path.split("/")[0]
            lst = top.split("[")[0]
            if lst not in OBJ_LISTS:
                continue
            rest = path[len(top) + 1:]
            o = by[(fid, top)]
            if rest.startswith("Geometry["):
                # a trigger's outline: points relative to its position
                j = int(rest[9:rest.index("]")])
                if label in ("PointX", "PointY"):
                    o.setdefault("geom", {}).setdefault(j, [0.0, 0.0])[label == "PointY"] = _float(value)
            elif "/" not in rest:
                o[label] = value
    objects = []
    for (fid, top), o in by.items():
        # doors use X/Y, waypoints and triggers XPosition/YPosition
        rec = dict(area=git[fid], kind=top.split("[")[0], path=top, tag=o.get("Tag") or "",
                   x=_float(o.get("XPosition") or o.get("X")), y=_float(o.get("YPosition") or o.get("Y")))
        for k in ("LinkedTo", "LinkedToFlags", "Type", "OnClick"):
            if o.get(k) not in (None, ""):
                rec[k] = o[k]
        if "geom" in o:
            rec["geom"] = [tuple(o["geom"][j]) for j in sorted(o["geom"])]
        objects.append(rec)
    for chunk in _chunks(list(git), 400):
        vt = defaultdict(dict)
        for fid, path, label, value in db.execute(
                f"SELECT file_id, path, label, value FROM fields WHERE file_id IN ({','.join('?' * len(chunk))}) "
                "AND path LIKE 'VarTable[%' AND label IN ('Name', 'Value')", tuple(chunk)):
            if path.count("/") == 1:                     # the area's own Variables, not an object's
                vt[(git[fid], path.split("/")[0])][label] = value
        for (rr, _top), v in vt.items():
            if rr in areas:
                areas[rr]["vars"].append([v.get("Name"), v.get("Value")])
    ifo = db.execute("SELECT id FROM files WHERE ext='ifo' AND source_id=? LIMIT 1", (mod,)).fetchone()
    entry = db.execute("SELECT value FROM fields WHERE file_id=? AND path='Mod_Entry_Area'", (ifo[0],)).fetchone() if ifo else None
    return areas, objects, dict(entry_area=(entry[0] or "").lower() if entry else "")


def _chunks(seq, n):
    """seq in slices of n (SQLite limits how many ? a statement may hold)."""
    return [seq[i:i + n] for i in range(0, len(seq), n)] or [[]]


def _int(v):
    """v as a whole number (GFF values come out of the index as text); 0 when it isn't one."""
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def _float(v):
    """v as a number; 0.0 when it isn't one."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


class Resources:
    """Finds and reads the copy of a resource the game uses, for an analysis: haks in module.ifo order, then the
    module, then the base game (nwnlib.source_rank). The builder's own override folder is left out on purpose: players
    don't have it, so a minimap image only there is still blank for them (the same rule as lean haks).

    db: the analysis index; nwn_root: the game folder (meta nwn_root) or None, used only to read base-game bytes.
    Archives are opened once and kept until close() (an open hak is locked on Windows - always close)."""

    def __init__(self, db, nwn_root=None):
        import nwnlib
        self.n = nwnlib
        self.db = db
        self.nwn_root = nwn_root
        self.sources = {sid: (path, kind) for sid, path, kind in db.execute("SELECT id, path, kind FROM sources")}
        self.base_names = {r[0] for r in db.execute("SELECT name FROM base_resources")}
        self._erf, self._base = {}, None
        self.unreadable = set()          # tilesets whose .set exists but could not be read (load_tilesets)

    @property
    def base_known(self):
        """True when the analysis read the base game's resource list (the game folder was set)."""
        return bool(self.base_names)

    def where(self, resref, exts):
        """(source description, ext) of the winning copy among exts, or None. Source description: the hak or folder
        name, or "base game"."""
        hit = self._winner(resref, exts)
        if hit:
            return os.path.basename(self.sources[hit[0]][0]), hit[2]
        for e in exts:
            if f"{resref}.{e}" in self.base_names:
                return "base game", e
        return None

    def _winner(self, resref, exts):
        """(source id, relpath, ext) of the winning indexed copy (no override), or None."""
        q = ",".join("?" * len(exts))
        order = " ".join(f"WHEN '{e}' THEN {i}" for i, e in enumerate(exts))
        row = self.db.execute(
            f"SELECT f.source_id, f.relpath, f.ext FROM files f JOIN sources s ON s.id = f.source_id "
            f"WHERE f.resref = ? AND f.ext IN ({q}) AND s.kind != 'override' "
            f"ORDER BY {self.n.source_rank_sql()}, CASE f.ext {order} END LIMIT 1", (resref, *exts)).fetchone()
        return row

    def read(self, resref, exts):
        """(bytes, ext) of the copy the game uses, or (None, None) when it can't be found or read. Never raises."""
        try:
            hit = self._winner(resref, exts)
            if hit:
                sid, rel, ext = hit
                path, _kind = self.sources[sid]
                if os.path.isdir(path):
                    return self.n.read_file_inside(path, rel), ext
                e = self._erf.get(sid)
                if e is None:
                    e = self._erf[sid] = self.n.Erf(path)
                    e.by_name = {x.filename: x for x in e.entries}
                ent = e.by_name.get(rel)
                return (e.read(ent), ext) if ent else (None, None)
            if self.nwn_root and any(f"{resref}.{x}" in self.base_names for x in exts):
                if self._base is None:
                    self._base = self.n.BaseGame(self.nwn_root)
                for x in exts:
                    b = self._base.get(f"{resref}.{x}")
                    if b:
                        return b, x
        except Exception:  # noqa - a damaged archive or entry: treated as not found
            pass
        return None, None

    def close(self):
        """Close every archive opened by read()."""
        for e in self._erf.values():
            try:
                e.close()
            except Exception:  # noqa
                pass
        self._erf = {}


def load_tilesets(areas, res):
    """{tileset: dict(tiles=parse_set(), general=parse_set_general(), where=source)} for every tileset the areas use;
    a tileset whose .set can't be found or read maps to None; one that exists but could not be read is also added to
    res.unreadable (so the check can say "damaged" instead of "nowhere")."""
    out = {}
    for ts in sorted({(a.get("Tileset") or "").lower() for a in areas.values()} - {""}):
        data, _ext = res.read(ts, ("set",))
        if data is None:
            out[ts] = None
            if res.where(ts, ("set",)) is not None:
                res.unreadable.add(ts)
            continue
        text = data.decode("latin-1", "replace")
        w = res.where(ts, ("set",))
        out[ts] = dict(tiles=parse_set(text), general=parse_set_general(text), where=w[0] if w else "?")
    return out


def env_scripts(db):
    """{script: [[line, text], ...]} - script lines that call GetIsAreaAboveGround (what an area's Underground
    setting changes), from the index's script sources."""
    out = defaultdict(list)
    for name, src in db.execute("SELECT name, source FROM scripts WHERE source LIKE '%GetIsAreaAboveGround%'"):
        for i, line in enumerate((src or "").splitlines(), 1):
            if ENV_CALL.search(line) and not line.strip().startswith("//"):
                out[name].append([i, line.strip()[:160]])
    return dict(out)


def missing_minimaps(areas, tilesets, res):
    """{area: render-style stats with "missing" {image: tiles}} for areas whose tiles name a minimap image that no
    hak, the module or the base game has - worked out from names only (no image is decoded)."""
    have = {}
    stats = {}
    for r, a in areas.items():
        t = tilesets.get((a.get("Tileset") or "").lower())
        if not t:
            continue
        W, H = _int(a.get("Width")), _int(a.get("Height"))
        miss = defaultdict(int)
        for tid, _o, _h in a["tiles"][:W * H]:
            name = (t["tiles"].get(tid) or {}).get("imagemap2d")
            if not name:
                continue
            if name not in have:
                have[name] = res.where(name, TEXTURE_EXTS) is not None
            if not have[name]:
                miss[name] += 1
        if miss:
            stats[r] = dict(tiles=W * H, missing=dict(miss))
    return stats


ISSUE_CATEGORY = {"env_underground_outdoor": "area_underground_outdoor",
                  "env_above_ground_cave": "area_above_ground_cave", "minimap_missing": "minimap_missing",
                  "tileset_missing": "area_tileset_missing", "tileset_unreadable": "area_tileset_unreadable",
                  "tile_unknown": "area_tile_unknown", "tile_model_missing": "area_tile_model_missing",
                  "tile_empty": "area_tile_empty"}


def area_checks(areas, tilesets, mm, res, haks_missing=(), holes=None):
    """Findings (transitions() format) about areas' tilesets, tiles and minimap images - the same list for the Issues
    page (map_checks) and the Map page (build_maps), so the two always agree.

    mm: missing_minimaps() result (names only). holes: tile_holes() result (worked out here when None). res: the
    Resources used (base_known, unreadable). haks_missing: hak
    names module.ifo lists that were not found. While the base game wasn't read or a listed hak is missing, a missing
    file may simply be there: such findings are notes that say so (the same rule as the other "missing" checks)."""
    hint = ""
    if haks_missing:
        hint += (f" {len(haks_missing)} hak(s) the module lists were not found ({', '.join(list(haks_missing)[:4])}"
                 f"{'...' if len(haks_missing) > 4 else ''}) - the missing files may be in them.")
    if not res.base_known:
        hint += (" The base game was not read (set the game folder in Settings and analyse again) - the missing files "
                 "may be there.")
    out = []
    src = {k: v["where"] for k, v in tilesets.items() if v}
    for f in minimap_issues(areas, mm, src):
        if hint:
            f["severity"] = "info"
            f["detail"] += hint
        out.append(f)
    out += hole_issues(areas, tile_holes(areas, tilesets, res) if holes is None else holes, src, hint)
    for r, a in sorted(areas.items()):
        ts = (a.get("Tileset") or "").lower()
        if not ts or tilesets.get(ts) is not None:
            continue
        if ts in res.unreadable:
            out.append(dict(severity="error", check="tileset_unreadable", area=r, object=f"tileset {ts}",
                            detail=f"tileset '{ts}' ({ts}.set) exists but could not be read - the file may be damaged.",
                            fix=f"Replace {ts}.set with a good copy (from the tileset's own download)."))
        else:
            out.append(dict(severity="info" if hint else "error", check="tileset_missing", area=r, object=f"tileset {ts}",
                            detail=f"tileset '{ts}' ({ts}.set) is not in any hak, the module or the base game - the area "
                                   "can't load." + hint,
                            fix=f"Add the hak that provides {ts}.set to the module (Module Properties > Custom Content), "
                                "or change the area's tileset."))
    return out


def _haks_missing(db):
    """Names of the haks module.ifo lists that the analysis could not find (meta haks_missing), or []."""
    try:
        row = db.execute("SELECT value FROM meta WHERE key='haks_missing'").fetchone()
        return [os.path.basename(str(x)) for x in json.loads(row[0] or "[]")] if row else []
    except Exception:  # noqa - an older index or a malformed value: no hint
        return []


def map_checks(db, nwn_root=None, label=None):
    """The map's checks as Issues-page issues, for nwn_analysis. Returns (issues, summary).

    db: the analysis index; nwn_root: the game folder (to read base-game tilesets); label(node) -> readable label
    (the analysis graph's), optional. issues: dict(severity, category, node, label, detail, fix) - transitions
    (category transition_<check>, node inst:<area>:<path>), areas set to Underground by mistake and the reverse,
    minimap images that don't exist, holes in an area's tiles (tile_holes), and areas whose tileset can't be found. summary: counts for report.json.
    Draws nothing (that is build_maps, on first use of the Map page). Never raises; a failure inside comes back as
    summary["error"]."""
    label = label or (lambda nid: nid)
    issues = []
    try:
        areas, objects, _module = load_world(db)
        res = Resources(db, nwn_root)
        try:
            tilesets = load_tilesets(areas, res)
            links, finds = transitions(areas, objects)
            envf = area_environment(areas, tilesets, links, env_scripts(db))
            mm = missing_minimaps(areas, tilesets, res)
            holes = tile_holes(areas, tilesets, res)
            area = area_checks(areas, tilesets, mm, res, _haks_missing(db), holes)
        finally:
            res.close()
        for f in finds:
            node = f"inst:{f['area']}:{f['path']}"
            issues.append(dict(severity=f["severity"], category="transition_" + f["check"], node=node,
                               label=label(node) if label(node) != node else
                               f"{f['object']} in {(areas.get(f['area']) or {}).get('Name') or f['area']}",
                               detail=f["detail"], fix=f["fix"]))
        for f in envf + area:
            node = "area:" + f["area"]
            issues.append(dict(severity=f["severity"], category=ISSUE_CATEGORY[f["check"]], node=node, label=label(node),
                               detail=f["detail"], fix=f["fix"]))
        summary = dict(areas=len(areas), transitions=len(links), edge_links=sum(1 for x in links if x["kind"] == "edge"),
                       minimap_missing_tiles=sum(sum(s["missing"].values()) for s in mm.values()),
                       minimap_missing_areas=len(mm), tile_hole_areas=len(holes))
    except Exception as ex:  # noqa - a check must never stop the analysis
        return issues, dict(error=f"map checks failed: {ex}")
    return issues, summary


# ============================================================================================ the Map page
def _open_index(analysis):
    """Read-only connection to <analysis>/index.sqlite and its meta {key: value}. nwnlib.sqlite_ro builds a proper
    file: URI, so a folder name with #, ? or % can't turn the open into a read-write one somewhere else."""
    import nwnlib
    db = nwnlib.sqlite_ro(os.path.join(analysis, "index.sqlite"))
    try:
        meta = dict(db.execute("SELECT key, value FROM meta"))
    except Exception:
        db.close()
        raise
    return db, meta


def _world(analysis):
    """Everything the map needs from an analysis: (db, meta, areas, objects, module, tilesets, links, findings,
    environment findings, Resources). The caller closes res and db."""
    db, meta = _open_index(analysis)
    res = None
    try:
        areas, objects, module = load_world(db)
        res = Resources(db, meta.get("nwn_root") or None)
        tilesets = load_tilesets(areas, res)
        links, finds = transitions(areas, objects)
        envf = area_environment(areas, tilesets, links, env_scripts(db))
    except BaseException:
        if res is not None:
            res.close()                  # an open hak stays locked on Windows
        db.close()
        raise
    return db, meta, areas, objects, module, tilesets, links, finds, envf, res


def _texture_fn(res):
    """texture(name) -> decoded minimap image or None, each image read and decoded once."""
    cache = {}

    def texture(name):
        if name not in cache:
            data, ext = res.read(name, TEXTURE_EXTS)
            cache[name] = decode_texture(data, ext) if data else None
        return cache[name]
    return texture


def _kind(areas, rs):
    """'Outdoor' / 'Underground' / 'Interior' for a group of areas: what most of them are (Flags bits 1 and 0)."""
    fl = [_int(areas[r].get("Flags")) for r in rs]
    if sum(f & 2 > 0 for f in fl) * 2 > len(fl):
        return "Underground"
    if sum(f & 1 > 0 for f in fl) * 2 > len(fl):
        return "Interior"
    return "Outdoor"


def build_maps(analysis, progress=None):
    """Draw every map of an analysis and save them: <analysis>/maps/map.json and maps/r<n>.png (one PNG per map: a
    stitched group of areas, or one area alone). Returns the map.json data.

    map.json: version, px, summary, regions [dict(i, kind, title, w, h, areas {resref: [x, y] in tiles from the
    south-west}, overlaps, loose, png)], areas {resref: dict(name, w, h, ts, flags, region, drawn, tiles, missing)},
    links [dict(s, d, k, side, o, ok, to, ps, pd)], checks [dict(sv, c, a, o, d, fx)] (every check, including the
    map-only note map_does_not_close), entry (the module's start area).
    progress(pct, msg): optional callback with a whole percentage and a sentence for the person waiting (the
    dashboard shows them in a pop-up; it may raise to stop the build - the half-built maps.tmp is then left for the
    next build to remove, and the old maps/ is untouched). Writes only into <analysis>/maps (via a temporary folder
    renamed into place); reads the index, the haks and the base game."""
    say = progress or (lambda pct, msg: None)
    say(0, "Reading the areas and their tilesets…")
    db, meta, areas, objects, module, tilesets, links, finds, envf, res = _world(analysis)
    try:
        fixed = {f["area"] for f in envf if f["check"] == "env_underground_outdoor"}
        regions, conflicts = layout(areas, links, treat_outdoor=fixed)
        texture = _texture_fn(res)
        canv, stats = {}, {}
        for k, (r, a) in enumerate(sorted(areas.items())):
            t = tilesets.get((a.get("Tileset") or "").lower())
            canv[r], stats[r] = render_area(a, t["tiles"] if t else None, texture, MAP_PX)
            if k % 5 == 0 or k + 1 == len(areas):
                # drawing the areas (reading and decoding their images) is most of the time: 0-90 %
                say(int((k + 1) * 90 / max(1, len(areas))), f"Drawing the areas: {k + 1} of {len(areas)} "
                                                            f"({a.get('Name') or r})")
        out_dir = os.path.join(analysis, "maps")
        tmp = out_dir + ".tmp"
        import nwnlib
        nwnlib.rmtree_force(tmp, ignore_errors=True)
        os.makedirs(tmp, exist_ok=True)
        R = []
        region_of = {}
        for i, rg in enumerate(regions):
            for r in rg["areas"]:
                region_of[r] = i
            if i % 10 == 0:
                say(90 + int(i * 9 / max(1, len(regions))), f"Putting the maps together: {i + 1} of {len(regions)}")
            img = render_region(rg, areas, canv, links, MAP_PX)
            with open(os.path.join(tmp, f"r{i}.png"), "wb") as fh:
                fh.write(img.png())
            # the map's name: its best-connected area (most edge links inside the map), biggest on a tie
            deg = defaultdict(int)
            for x in links:
                if x["kind"] == "edge" and x["src_area"] in rg["areas"] and x["dst_area"] in rg["areas"]:
                    deg[x["src_area"]] += 1
            hub = sorted(rg["areas"], key=lambda r: (-deg[r], -_int(areas[r]["Width"]) * _int(areas[r]["Height"]), r))[0]
            R.append(dict(i=i, kind=_kind(areas, rg["areas"]), w=rg["w"], h=rg["h"], png=f"r{i}.png",
                          title=areas[hub].get("Name") or hub,
                          areas={r: list(xy) for r, xy in rg["areas"].items()},
                          overlaps=rg.get("overlaps", []), loose=rg.get("loose", [])))
        A = {r: dict(name=a.get("Name") or r, w=_int(a.get("Width")), h=_int(a.get("Height")), ts=a.get("Tileset") or "",
                     flags=_int(a.get("Flags")), region=region_of.get(r, 0), drawn=stats[r]["drawn"],
                     tiles=stats[r]["tiles"], missing=stats[r].get("missing", {}),
                     no_set=stats[r]["no_set"], holes=stats[r]["holes"]) for r, a in areas.items()}
        L = [dict(s=x["src_area"], d=x["dst_area"], k=x["kind"], side=x["side"], o=x["src"]["tag"] or x["src"]["path"],
                  ok=x["src"]["kind"].replace(" List", "").replace("List", ""), to=x["dst"]["tag"],
                  ps=[round(v, 1) for v in x["p_src"]], pd=[round(v, 1) for v in x["p_dst"]]) for x in links]
        # missing images by name (as the Issues page has them); an image that exists but couldn't be decoded (a TGA
        # or DDS variant this reader doesn't know) is grey here too, but only a note: the game may well show it
        mm = missing_minimaps(areas, tilesets, res)
        allf = finds + envf + area_checks(areas, tilesets, mm, res, _haks_missing(db))
        for r, s_ in sorted(stats.items()):
            bad = {k: v for k, v in (s_.get("missing") or {}).items() if k not in (mm.get(r) or {}).get("missing", {})}
            if bad:
                allf.append(dict(severity="info", check="minimap_unreadable", area=r, object="minimap images",
                                 detail=f"{sum(bad.values())} tile(s) drawn grey: their minimap image exists but this "
                                        f"page can't read its format ({', '.join(sorted(bad))}). The game may show "
                                        "them; nothing is known to be wrong.",
                                 fix="Nothing to do unless the in-game minimap is blank there; if it is, re-save the "
                                     "image as a 24/32-bit TGA."))
        C = [dict(sv=f["severity"], c=f["check"], a=f["area"], o=f["object"], d=f["detail"], fx=f["fix"]) for f in allf]
        C += [dict(sv="info", c="map_does_not_close", a=c["area"], o=(areas.get(c["other"]) or {}).get("Name") or c["other"],
                   d=c["detail"], fx="Usually harmless (areas joined in a loop rarely line up exactly); for one seamless "
                                     "map, move the transitions so each arrival sits opposite its departure.")
              for c in conflicts]
        tiles = sum(s["tiles"] for s in stats.values())
        drawn = sum(s["drawn"] for s in stats.values())
        summary = dict(areas=len(areas), maps=sum(1 for x in R if len(x["areas"]) > 1),
                       stitched=sum(len(x["areas"]) for x in R if len(x["areas"]) > 1), links=len(L),
                       edge=sum(1 for x in L if x["k"] == "edge"), tiles=tiles, drawn=drawn,
                       missing_tiles=sum(sum(s_["missing"].values()) for s_ in mm.values()),
                       no_set=sum(s["no_set"] for s in stats.values()), base_game=res.base_known,
                       holes=sum(s["holes"] for s in stats.values()),
                       tilesets_missing=sorted(k for k, v in tilesets.items() if v is None))
        data = dict(version=MAP_VERSION, px=MAP_PX, summary=summary, regions=R, areas=A, links=L, checks=C,
                    entry=module.get("entry_area", ""))
        say(99, "Saving the maps…")
        with open(os.path.join(tmp, "map.json"), "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, separators=(",", ":"))
        # swap in the new maps: the old folder is first renamed aside (renaming to a free name works on Windows even
        # while a PNG in it is open; replacing a folder that still exists does not), then the new one takes its place
        import nwnlib
        old = out_dir + ".old"
        nwnlib.rmtree_force(old, ignore_errors=True)
        if os.path.isdir(out_dir):
            os.rename(out_dir, old)
        os.rename(tmp, out_dir)
        nwnlib.rmtree_force(old, ignore_errors=True)
        return data
    finally:
        res.close()
        db.close()


def export_png(analysis, region=None, area=None, px=EXPORT_PX):
    """PNG bytes of one map (region number from map.json) or one area (resref) at px pixels per tile, drawn now (for
    the Map page's Export). Raises ValueError for an unknown map or area. Read-only."""
    db, meta, areas, objects, module, tilesets, links, finds, envf, res = _world(analysis)
    try:
        texture = _texture_fn(res)
        if area is not None:
            a = areas.get(str(area).lower())
            if a is None:
                raise ValueError(f"no area '{area}'")
            t = tilesets.get((a.get("Tileset") or "").lower())
            return render_area(a, t["tiles"] if t else None, texture, px)[0].png()
        fixed = {f["area"] for f in envf if f["check"] == "env_underground_outdoor"}
        regions, _c = layout(areas, links, treat_outdoor=fixed)
        if not isinstance(region, int) or not 0 <= region < len(regions):
            raise ValueError("no such map")
        rg = regions[region]
        canv = {}
        for r in rg["areas"]:
            t = tilesets.get((areas[r].get("Tileset") or "").lower())
            canv[r] = render_area(areas[r], t["tiles"] if t else None, texture, px)[0]
        return render_region(rg, areas, canv, links, px).png()
    finally:
        res.close()
        db.close()
