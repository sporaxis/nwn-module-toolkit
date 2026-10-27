"""
Tests for the Map page and the map checks (nwn_minimap; the owner: "produce a map of all linked areas, using the
minimaps as an image source ... areas that are linked edge wise ... and a linked area transition checker"; "the missing
minimap should also be considered an error and shown in the issues list"; "areas set to Underground that are above
ground as a finding").
  - drawing: a tile's minimap image into its cell, turned by Tile_Orientation (90 degrees counter-clockwise each),
    images from a hak (TGA) and from the base game; a tile with no image drawn grey and counted;
  - layout: two areas joined edge to edge become one map, side by side;
  - checks on the Issues page: destination missing / wrong capitals / link switched off, a missing minimap image
    (error; a note when the base game wasn't read), an area set to Underground on an exterior tileset with weather,
    a tileset nobody has, holes in an area's tiles (a tile number the tileset lacks, a tile model nobody has or none
    named, tile positions the tile list doesn't fill - a tester's report, 2026-10-27), each hole drawn dark red;
  - the dashboard: /api/map draws once and keeps maps/, /api/map/png, Export as PNG.
    python tests/test_map.py          (last line "N/M checks passed"; exit code 1 on a failure)
Works only in temporary folders and the test workspace (see tests/_harness.py).
"""
import _harness as h  # noqa: F401 - first: isolates the workspace and the compiler before toolkit modules load
import os
import shutil
import struct
import sys

import nwnlib as n  # noqa: E402
import nwn_dashboard as D  # noqa: E402
import nwn_minimap as M  # noqa: E402
import nwn_update  # noqa: E402
from _harness import check  # noqa: E402
from _gff import st, root, loc, w, tga, R, X, B, I, F, DW, LST  # noqa: E402

GREEN, ROCK, RED, BLUE, BASEC = (40, 160, 60), (90, 80, 70), (220, 30, 30), (30, 60, 220), (200, 200, 40)


def set_text(name, interior, tiles):
    """A tileset .set: [GENERAL] and one [TILEn] per (model, minimap image)."""
    out = f"[GENERAL]\nName={name}\nType=SET\nInterior={interior}\nUnlocalizedName={name} test\n\n[TILES]\nCount={len(tiles)}\n"
    for i, (model, im) in enumerate(tiles):
        out += f"\n[TILE{i}]\nModel={model}\nImageMap2D={im}\n"
    return out


def area(fname_dir, rr, name, ts, wdt, hgt, tiles, flags=0, **extra):
    """An .are with a tile list [(Tile_ID, Tile_Orientation)] in .are order (row by row from the south-west)."""
    tl = [st(1, Tile_ID=(I, t), Tile_Orientation=(I, o), Tile_Height=(I, 0)) for t, o in tiles]
    w(fname_dir, rr + ".are", n.write_gff(root("ARE ", Name=loc(name), Tag=(X, rr.upper()), ResRef=(R, rr),
                                                  Tileset=(R, ts), Width=(I, wdt), Height=(I, hgt), Flags=(DW, flags),
                                                  Tile_List=(LST, tl), **extra)))


def wp(tag, x, y):
    return st(5, Tag=(X, tag), XPosition=(F, x), YPosition=(F, y), ZPosition=(F, 0.0), TemplateResRef=(R, "nw_waypoint001"))


def trig(tag, x, y, to, flags=2, size=4.0):
    """An area-transition trigger: a square outline of `size` metres centred on (x, y)."""
    s2 = size / 2
    geom = [st(3, PointX=(F, px), PointY=(F, py), PointZ=(F, 0.0)) for px, py in ((-s2, -s2), (s2, -s2), (s2, s2), (-s2, s2))]
    return st(1, Tag=(X, tag), XPosition=(F, x), YPosition=(F, y), ZPosition=(F, 0.0), Type=(I, 1), LinkedTo=(X, to),
              LinkedToFlags=(B, flags), Geometry=(LST, geom), TemplateResRef=(R, "newtransition"))


def door(tag, x, y, to, flags=1):
    return st(8, Tag=(X, tag), X=(F, x), Y=(F, y), Z=(F, 0.0), LinkedTo=(X, to), LinkedToFlags=(B, flags),
              TemplateResRef=(R, "door_iron"))


def fixture(tmp):
    """A module with five areas, a hak with two tilesets and a fake game install with a third.
    field_w | field_e  (2 x 2 tiles each, outdoor tileset tout from the hak, joined by triggers at the shared edge)
    hills   (tout, but set to Underground, with rain, a day/night cycle and a sky box)
    cave    (tcave from the hak: one tile's minimap image exists nowhere)
    crypt   (tbase from the base game; its image is read from the game's data)
    lost    (tileset nosuch: nobody has it)
    holey   (thole from the hak, 3 x 2 tiles but only 5 in its list: tile 7 that thole doesn't have, a tile whose
            model nobody has, a tile whose .set section names no model, and one empty position)"""
    mod = os.path.join(tmp, "mod"); hakd = os.path.join(tmp, "hakfiles"); groot = os.path.join(tmp, "game")
    for d in (mod, hakd):
        os.makedirs(d)
    # the hak: two tilesets and their minimap images (tc_room has none anywhere)
    w(hakd, "tout.set", set_text("tout", 0, [("tout_a01_01", "mi_tout_grass"), ("tout_a02_01", "mi_tout_split"),
                                             ("tout_a03_01", "mi_tout_odd")]))
    # a 16-bit TGA: it exists, but this reader can't decode it (drawn grey on the Map page, never reported as missing)
    w(hakd, "mi_tout_odd.tga", bytes([0, 0, 2, 0, 0, 0, 0, 0, 0, 0, 0, 0]) + struct.pack("<HH", 2, 2) + bytes([16, 0]) + b"\0" * 8)
    w(hakd, "tcave.set", set_text("tcave", 1, [("tcave_a01_01", "mi_tcave_rock"), ("tcave_r01_01", "mi_tcave_room")]))
    w(hakd, "mi_tout_grass.tga", tga(8, 8, lambda x, y: GREEN))
    # left half red, right half blue: shows which way a turned tile faces
    w(hakd, "mi_tout_split.tga", tga(8, 8, lambda x, y: RED if x < 4 else BLUE))
    w(hakd, "mi_tcave_rock.tga", tga(8, 8, lambda x, y: ROCK))
    # every tile model the tilesets name exists (so only holey's holes are reported); an ASCII model is enough here
    for m in ("tout_a01_01", "tout_a02_01", "tout_a03_01", "tcave_a01_01", "tcave_r01_01", "thole_a01_01"):
        w(hakd, m + ".mdl", f"newmodel {m}\nbeginmodelgeom {m}\nendmodelgeom {m}\ndonemodel {m}\n")
    # thole: tile 0 is fine, tile 1's model exists nowhere, tile 2's section has no Model line
    w(hakd, "thole.set", set_text("thole", 0, [("thole_a01_01", "mi_tout_grass"), ("thole_gone_01", "mi_tout_grass")])
      + "\n[TILE2]\nImageMap2D=mi_tout_grass\n")
    hak = os.path.join(tmp, "tiles.hak")
    h.pack_hak(hakd, hak)
    n.write_key_bif(groot, {("tbase", "set"): set_text("tbase", 1, [("tbase_a01_01", "mi_tbase_floor")]).encode(),
                            ("tbase_a01_01", "mdl"): b"newmodel tbase_a01_01\ndonemodel tbase_a01_01\n",
                            ("mi_tbase_floor", "tga"): tga(8, 8, lambda x, y: BASEC)})
    g = lambda fn, r: w(mod, fn, n.write_gff(r))  # noqa: E731
    names = ["field_w", "field_e", "hills", "cave", "crypt", "lost", "broken", "holey"]
    g("module.ifo", root("IFO ", Mod_Name=loc("Map test"), Mod_Entry_Area=(R, "field_w"),
                         Mod_Area_list=(LST, [st(6, Area_Name=(R, a)) for a in names]),
                         Mod_HakList=(LST, [st(8, Mod_Hak=(X, "tiles"))])))
    # field_w: tile 3 (north-east) is the split image turned once (90 degrees counter-clockwise)
    area(mod, "field_w", "West Field", "tout", 2, 2, [(0, 0), (0, 0), (0, 0), (1, 1)], ChanceRain=(I, 20), DayNightCycle=(B, 1))
    area(mod, "field_e", "East Field", "tout", 2, 2, [(0, 0)] * 4, DayNightCycle=(B, 1))
    area(mod, "hills", "Windy Hills", "tout", 2, 2, [(0, 0)] * 3 + [(2, 0)], flags=2 | 4, ChanceRain=(I, 50), DayNightCycle=(B, 1),
         SkyBox=(B, 2))
    area(mod, "cave", "Old Cave", "tcave", 2, 1, [(0, 0), (1, 0)], flags=2 | 1)
    area(mod, "crypt", "Crypt", "tbase", 1, 1, [(0, 0)], flags=1 | 2)
    area(mod, "lost", "Lost Place", "nosuch", 1, 1, [(0, 0)], flags=1)
    area(mod, "holey", "Holey Ground", "thole", 3, 2, [(0, 0), (1, 0), (2, 0), (7, 0), (0, 0)], flags=1)
    # field_w (20 x 20 m): a trigger at its east edge to WP_E_IN at field_e's west edge, and back; plus broken ones
    g("field_w.git", root("GIT ", **{
        "TriggerList": (LST, [trig("TO_EAST", 19.0, 10.0, "WP_E_IN"), trig("TO_CAVE", 10.0, 2.0, "wp_cave_in"),
                              trig("TO_HILLS", 2.0, 10.0, "WP_HILLS", flags=0)]),
        "Door List": (LST, [door("DOOR_NOWHERE", 5.0, 15.0, "NO_SUCH_TAG")]),
        "WaypointList": (LST, [wp("WP_W_IN", 18.0, 10.0)])}))
    g("field_e.git", root("GIT ", **{"TriggerList": (LST, [trig("TO_WEST", 1.0, 10.0, "WP_W_IN")]),
                                     "WaypointList": (LST, [wp("WP_E_IN", 2.0, 10.0)])}))
    g("hills.git", root("GIT ", **{"WaypointList": (LST, [wp("WP_HILLS", 10.0, 10.0)])}))
    g("cave.git", root("GIT ", **{"WaypointList": (LST, [wp("WP_CAVE_IN", 5.0, 5.0)])}))
    for a in ("crypt", "lost", "holey"):
        g(f"{a}.git", root("GIT "))
    # a damaged area file: indexed as a file, no fields (the Map page must still open)
    w(mod, "broken.are", b"ARE V3.2 this is not a real GFF")
    # a script that asks whether an area is above ground: named in the Underground finding as what the setting changes
    w(mod, "sky_setup.nss", "void main()\n{\n    object oArea = GetFirstArea();\n    if (GetIsAreaAboveGround(oArea))\n"
                           "        SetSkyBox(18, oArea);\n}\n")
    return mod, hak, groot


def issues_by(rep, cat):
    return [i for i in rep["issues"] if i["category"] == cat]


def checks(tmp):
    mod, hak, groot = fixture(tmp)
    a = "map_t"
    out = os.path.join(D.WORKSPACE, a)
    try:
        rep = h.analyse(mod, out, haks=[hak], nwn_root=groot)
        check("report: map_checks marks the analysis (no 'analyse again' for it)",
              rep.get("map_checks") == 1 and not [x for x in nwn_update.missing_features(rep) if x["key"] == "map_checks"],
              rep.get("map_summary"))
        sm = rep.get("map_summary") or {}
        check("report: map summary counts areas and edge links", sm.get("areas") == 8 and sm.get("edge_links") == 2, sm)
        dm = issues_by(rep, "transition_dest_missing")
        check("issue: a door to a tag nothing has is an error, on the door's node, with a fix",
              len(dm) == 1 and dm[0]["severity"] == "error" and dm[0]["node"] == "inst:field_w:Door List[0]"
              and "NO_SUCH_TAG" in dm[0]["detail"] and dm[0].get("fix"), dm)
        dc = issues_by(rep, "transition_dest_case")
        check("issue: a destination that differs only in capitals is an error naming the real tag and its area",
              len(dc) == 1 and dc[0]["severity"] == "error" and "WP_CAVE_IN" in dc[0]["detail"] and "Old Cave" in dc[0]["detail"], dc)
        nl = issues_by(rep, "transition_no_link_flag")
        check("issue: a transition whose 'Link to' is None (LinkedToFlags 0) is a warning",
              len(nl) == 1 and nl[0]["severity"] == "warning" and "TO_HILLS" in nl[0]["label"] + nl[0]["node"] + nl[0]["detail"]
              or (len(nl) == 1 and nl[0]["node"].startswith("inst:field_w:TriggerList[2]")), nl)
        mm = issues_by(rep, "minimap_missing")
        check("issue: a tile whose minimap image exists nowhere is an ERROR on the area, naming the image, the hak "
              "and the tile count", len(mm) == 1 and mm[0]["severity"] == "error" and mm[0]["node"] == "area:cave"
              and "mi_tcave_room (1)" in mm[0]["detail"] and "tiles.hak" in mm[0]["detail"] and "1 of 2 tiles" in mm[0]["detail"], mm)
        check("issue: images found in the hak or the base game are not reported",
              not [i for i in mm if i["node"] in ("area:field_w", "area:crypt")])
        ug = issues_by(rep, "area_underground_outdoor")
        check("issue: an area set to Underground on an exterior tileset with weather and a sky box is a warning that "
              "names the evidence and the script line that reads the setting",
              len(ug) == 1 and ug[0]["node"] == "area:hills" and ug[0]["severity"] == "warning" and "rain 50%" in ug[0]["detail"]
              and "sky box" in ug[0]["detail"] and "sky_setup line 4" in ug[0]["detail"], ug)
        check("issue: the real cave (interior/underground tileset) is not flagged", not [i for i in ug if i["node"] == "area:cave"])
        tm = issues_by(rep, "area_tileset_missing")
        check("issue: an area whose tileset nobody has is an error", len(tm) == 1 and tm[0]["node"] == "area:lost"
              and tm[0]["severity"] == "error", tm)
        tu = issues_by(rep, "area_tile_unknown")
        check("issue: a tile number the tileset doesn't have is an ERROR on the area, naming the number, the count and "
              "where the .set came from", len(tu) == 1 and tu[0]["node"] == "area:holey" and tu[0]["severity"] == "error"
              and "7 (1)" in tu[0]["detail"] and "1 of 6 tiles" in tu[0]["detail"] and "tiles.hak" in tu[0]["detail"]
              and tu[0].get("fix"), tu)
        tmm = issues_by(rep, "area_tile_model_missing")
        check("issue: tiles whose model nobody has, or whose .set names none, are an error naming each",
              len(tmm) == 1 and tmm[0]["node"] == "area:holey" and tmm[0]["severity"] == "error"
              and "thole_gone_01 (1)" in tmm[0]["detail"] and "(none) (1)" in tmm[0]["detail"]
              and "2 of 6 tiles" in tmm[0]["detail"], tmm)
        te = issues_by(rep, "area_tile_empty")
        check("issue: tile positions the tile list doesn't fill are an error (1 of 3 x 2 empty)",
              len(te) == 1 and te[0]["node"] == "area:holey" and te[0]["severity"] == "error"
              and "1 of 6 tile positions" in te[0]["detail"] and "tile list has 5" in te[0]["detail"], te)
        check("issue: areas whose tiles and models all exist have no tile issues (models in the hak and the game)",
              not [i for i in tu + tmm + te if i["node"] != "area:holey"])
        check("report: tile_checks marks the analysis and the summary counts the area with holes",
              rep.get("tile_checks") == 1 and sm.get("tile_hole_areas") == 1, sm)

        # ---- the Map page: drawn on first use into maps/ by a job (pop-up with progress), then reused
        first = D.api_map({"a": a})
        check("api: the first /api/map starts a drawing job instead of a silent wait", first.get("building") is True and first.get("job"), first)
        again = D.api_map({"a": a})
        check("api: asked again while it draws, the same job is returned (never two builds)",
              again.get("job") == first.get("job") or again.get("version") == M.MAP_VERSION, again)
        import time as _t
        for _ in range(600):
            jj = D.api_job({"id": first["job"]})
            if jj["status"] != "running":
                break
            _t.sleep(0.05)
        check("job: it ends done, its last message says what it was doing, at 99-100 %",
              jj["status"] == "done" and jj.get("pct", 0) >= 99 and jj.get("msg"), {k: jj.get(k) for k in ("status", "pct", "msg", "lines")})
        data = D.api_map({"a": a})
        check("api: once drawn, /api/map sends the map itself", data.get("version") == M.MAP_VERSION and "building" not in data)
        mp = os.path.join(out, "maps", "map.json")
        check("map: map.json and one PNG per map written under maps/", os.path.isfile(mp) and all(
            os.path.isfile(os.path.join(out, "maps", r["png"])) for r in data["regions"]), os.listdir(os.path.join(out, "maps")))
        reg = data["regions"][data["areas"]["field_w"]["region"]]
        check("map: the two fields joined edge to edge share a map, East Field 2 tiles east of West Field",
              set(reg["areas"]) == {"field_w", "field_e"} and reg["areas"]["field_e"][0] - reg["areas"]["field_w"][0] == 2
              and reg["areas"]["field_e"][1] == reg["areas"]["field_w"][1], reg)
        check("map: the cave and the hills get maps of their own", data["areas"]["cave"]["region"] != data["areas"]["field_w"]["region"])
        ca = data["areas"]["cave"]
        check("map: the tile with no image is counted as missing, the other drawn", ca["drawn"] == 1 and ca["missing"] == {"mi_tcave_room": 1}, ca)
        check("map: the base game's tileset and image are read from the game's data (crypt drawn)",
              data["areas"]["crypt"]["drawn"] == 1, data["areas"]["crypt"])
        check("map: the checks list carries the same findings (minimap_missing error, tileset missing)",
              any(c["c"] == "minimap_missing" and c["sv"] == "error" and c["a"] == "cave" for c in data["checks"]) and
              any(c["c"] == "tileset_missing" and c["a"] == "lost" for c in data["checks"]))
        hl = data["areas"]["hills"]
        check("map: an image that exists but can't be decoded is grey and a note - not 'missing' anywhere",
              hl["drawn"] == 3 and any(c["c"] == "minimap_unreadable" and c["sv"] == "info" and c["a"] == "hills" for c in data["checks"])
              and not [i for i in issues_by(rep, "minimap_missing") if i["node"] == "area:hills"], hl)
        ho = data["areas"]["holey"]
        check("map: the holes are counted (a tile the tileset lacks + the empty position); the 4 others drawn, those "
              "with a missing model too (they still have a minimap image)",
              ho["holes"] == 2 and ho["drawn"] == 4 and data["summary"]["holes"] == 2, ho)
        check("map: the checks list carries the tile findings too",
              {c["c"] for c in data["checks"] if c["a"] == "holey"} >= {"tile_unknown", "tile_model_missing", "tile_empty"})
        check("map: a damaged area file doesn't stop the Map page (drawn as an empty area)",
              "broken" in data["areas"] and data["areas"]["broken"]["tiles"] == 0, data["areas"].get("broken"))
        t0 = os.path.getmtime(mp)
        D.map_data(out)
        check("map: opened again, the saved map is reused (not redrawn)", os.path.getmtime(mp) == t0)

        # ---- pixels: images in their cells, turned the right way
        db, meta, areas, objects, module, tilesets, links, finds, envf, res = M._world(out)
        try:
            tex = M._texture_fn(res)
            cv, stt = M.render_area(areas["field_w"], tilesets["tout"]["tiles"], tex, 8)
            cvc, _ = M.render_area(areas["crypt"], tilesets["tbase"]["tiles"], tex, 8)
            cvx, _ = M.render_area(areas["cave"], tilesets["tcave"]["tiles"], tex, 8)
            cvh, sth = M.render_area(areas["holey"], tilesets["thole"]["tiles"], tex, 8)
        finally:
            res.close(); db.close()
        px = lambda c, x, y: tuple(c.px[(y * c.w + x) * 3:(y * c.w + x) * 3 + 3])  # noqa: E731
        check("draw: the south-west tile (first in the list) sits bottom-left with its image", px(cv, 2, 12) == GREEN)
        # the split image (red left, blue right) turned 90 degrees counter-clockwise: red at the bottom, blue on top
        check("draw: Tile_Orientation 1 turns the image 90 degrees counter-clockwise (north-east tile: blue top, red bottom)",
              px(cv, 12, 1) == BLUE and px(cv, 12, 6) == RED, (px(cv, 12, 1), px(cv, 12, 6)))
        check("draw: a base-game image is drawn", px(cvc, 4, 4) == BASEC)
        check("draw: a tile with no image is plain light grey (never black, which the game uses for rock)",
              px(cvx, 12, 4) == M.MISSING_FILL and px(cvx, 4, 4) == ROCK)
        # holey, 3 x 2 at 8 px: the 4th tile (x 0, north row) is tile 7, the 6th position (x 2, north row) is empty
        check("draw: a hole is dark red and each tile is drawn on its own (one hole doesn't blank the area)",
              px(cvh, 4, 4) == M.HOLE_FILL and px(cvh, 20, 4) == M.HOLE_FILL and px(cvh, 4, 12) == GREEN
              and px(cvh, 12, 4) == GREEN and sth["holes"] == 2, (px(cvh, 4, 4), px(cvh, 20, 4), sth))

        # ---- dashboard endpoints
        i = data["areas"]["field_w"]["region"]
        r1 = D.api_map_png({"a": a, "r": str(i)})
        check("api: /api/map/png sends the map's picture", r1.code == 200 and r1.body[:8] == b"\x89PNG\r\n\x1a\n")
        check("api: /api/map/png refuses anything but a map number", D.api_map_png({"a": a, "r": "../x"}).code == 400)
        ex = D.api_map_export({"a": a, "area": "field_w"})
        wpx = struct.unpack(">I", ex.body[16:20])[0] if ex.code == 200 else 0
        check("api: Export draws an area at 16 pixels per tile as a download", ex.code == 200 and wpx == 32
              and "attachment" in ex.extra.get("Content-Disposition", ""), (ex.code, wpx))
        ex2 = D.api_map_export({"a": a, "r": str(i)})
        check("api: Export of a whole map", ex2.code == 200 and struct.unpack(">I", ex2.body[16:20])[0] == 64)
        check("api: Export of an unknown area is a 404, a bad name a 400",
              D.api_map_export({"a": a, "area": "nope"}).code == 404 and D.api_map_export({"a": a, "area": "../x"}).code == 400)
        check("clear: maps/ is data an analysis run generates (Clear removes it)", "maps" in D.DATA_DIRS and "maps.old" in D.DATA_DIRS)
        # drawn again over an existing maps/ (a re-analysis): the old folder is swapped out, nothing left beside it
        os.utime(os.path.join(out, "index.sqlite"))
        D.map_data(out)
        check("map: drawing again replaces maps/ and leaves no maps.tmp / maps.old behind",
              os.path.isfile(mp) and not os.path.exists(os.path.join(out, "maps.tmp")) and not os.path.exists(os.path.join(out, "maps.old")))
        # a workspace folder with # and % in its name: the index is still opened read-only, in place
        odd = os.path.join(tmp, "ws #2 100%")
        os.makedirs(odd)
        shutil.copy(os.path.join(out, "index.sqlite"), os.path.join(odd, "index.sqlite"))
        before = set(os.listdir(tmp))
        db2, meta2 = M._open_index(odd)
        db2.close()
        check("index: a folder name with # or % opens the right file read-only and creates nothing",
              meta2.get("module_name") is not None and set(os.listdir(tmp)) == before, sorted(set(os.listdir(tmp)) - before))
        page = open(os.path.join(h.ROOT, "dashboard.html"), encoding="utf-8").read()
        check("page: a Map page in the navigation", '["map","Map"]' in page and "VIEWS.map = async" in page)
        check("page: a module with no areas gets a sentence, not an endless redraw", "if (!R.length){" in page)
        check("page: the first drawing shows a pop-up with progress and Stop (jobModal)", 'jobModal("Drawing the map…", D_.job)' in page)
        msgs = []
        M.build_maps(out, progress=lambda p_, m_: msgs.append((p_, m_)))
        check("progress: build_maps reports rising percentages with a sentence each, ending at 99",
              msgs and all(msgs[i][0] <= msgs[i + 1][0] for i in range(len(msgs) - 1)) and msgs[-1][0] == 99
              and any("Drawing the areas" in m_ for _p, m_ in msgs), msgs[:3] + msgs[-2:])
    finally:
        D.release_analysis(a)
        shutil.rmtree(out, ignore_errors=True)

    # without the base game the same missing image may be in the game files: a note that says so, not an error
    b = "map_nobase"
    out2 = os.path.join(D.WORKSPACE, b)
    try:
        rep2 = h.analyse(mod, out2, haks=[hak])
        mm2 = issues_by(rep2, "minimap_missing")
        check("issue: without the base game read, a missing minimap image is a note that says why",
              mm2 and all(x["severity"] == "info" and "base game was not read" in x["detail"] for x in mm2), mm2)
        tm2 = issues_by(rep2, "area_tileset_missing")
        check("issue: ... and so is a tileset that may be in the base game", tm2 and all(x["severity"] == "info" for x in tm2), tm2)
        tmm2 = issues_by(rep2, "area_tile_model_missing")
        check("issue: ... and a tile model that may be in the base game; a tile number the tileset lacks stays an error",
              tmm2 and all(x["severity"] == "info" and "base game was not read" in x["detail"] for x in tmm2)
              and all(x["severity"] == "error" for x in issues_by(rep2, "area_tile_unknown")), tmm2)
        d2 = D.map_data(out2)
        check("map: the Map page agrees with the Issues page (notes, not errors, without the base game)",
              all(c["sv"] == "info" for c in d2["checks"] if c["c"] in ("minimap_missing", "tileset_missing")), d2["checks"])
    finally:
        D.release_analysis(b)
        shutil.rmtree(out2, ignore_errors=True)
    # the module's hak not found: every tileset in it is "missing" - a note naming the missing hak, not an error
    c_ = "map_nohak"
    out3 = os.path.join(D.WORKSPACE, c_)
    try:
        rep3 = h.analyse(mod, out3, nwn_root=groot)
        tm3 = issues_by(rep3, "area_tileset_missing")
        check("issue: a listed hak not found -> tileset missing is a note that names the hak",
              tm3 and all(x["severity"] == "info" and "tiles" in x["detail"] and "were not found" in x["detail"] for x in tm3), tm3)
    finally:
        D.release_analysis(c_)
        shutil.rmtree(out3, ignore_errors=True)


def main():
    """Run every group of checks; returns the exit code (tests/_harness.summary)."""
    with h.tempdir() as t:
        h.run(checks, t)
    return h.summary()


if __name__ == "__main__":
    sys.exit(main())
