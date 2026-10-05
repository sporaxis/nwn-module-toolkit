"""
nwn_dupdiff.py - how two look-alike blueprints actually differ, field by field, grouped by what a player would notice.

    look       model parts, colours (cloth/leather/metal/tattoo/hair/skin), appearance, portrait, phenotype, tail, wings,
               visual transform, sound set - swapping these changes what players SEE
    name       name and descriptions
    tag        the tag scripts look it up by
    stats      item properties, base item, cost, charges, stack size, abilities, classes, feats, skills, AC, HP ...
    scripts    event scripts and conversation
    inventory  what it carries or sells
    variables  toolset variables (VarTable)
    other      anything else
Identity fields (resref, template, comment, palette slot) are ignored - they always differ.

Why: the duplicate finder groups blueprints that look alike; before two are merged, the builder needs to see whether the
difference is cosmetic (a colour) or matters to play (a tag a script looks up, a different OnDeath script).

Reads the GFF fields stored in the analysis index (the `fields` table) - nothing else, and writes nothing.
Used by nwn_analysis.py (the "differs" chips on the Duplicates page) and the dashboard's compare view.
Limits: classification is by field name only (first matching class in CLASSES wins), so it is a guide, not proof.
Paths that exist in only one blueprint (an extra inventory item) show as a difference with None on the other side.
"""
from __future__ import annotations

import re

IGNORE = {"TemplateResRef", "ResRef", "Comment", "PaletteID"}
# (class, regex on the GFF field path), tried in this order - the first match wins. Order matters: an item's
# inventory or VarTable entry must be classed as inventory/variables even when its own field name (e.g. "Tag")
# would also match a later class. Example: "ItemList[2]/InventoryRes" -> inventory, "Appearance_Type" -> look.
CLASSES = [
    ("inventory", re.compile(r"(^|/)(ItemList|Equip_ItemList|StoreList)\[")),
    ("variables", re.compile(r"(^|/)VarTable\[")),
    ("look", re.compile(r"ModelPart|Color|ArmorPart|Appearance|Portrait|Tail|Wings|Phenotype|VisualTransform|"
                        r"ModelVariation|BodyPart|BodyBag|SoundSetFile|Head$|Gender$|Animation", re.I)),
    ("name", re.compile(r"(^|/)(LocName|LocalizedName|FirstName|LastName|Description|DescIdentified|Name)$")),
    ("tag", re.compile(r"(^|/)Tag$")),
    ("scripts", re.compile(r"(^|/)(On\w+|Script\w*|Conversation)$")),
    ("stats", re.compile(r"PropertiesList|BaseItem|Cost|Charges|StackSize|Plot|Stolen|Cursed|Identified|Str$|Dex$|Con$|"
                         r"Int$|Wis$|Cha$|HitPoints|ChallengeRating|ClassList|FeatList|SkillList|NaturalAC|Faction|"
                         r"SpecAbilityList|Hardness|Fort$|Ref$|Will$|HP$|Race$|WalkRate|PerceptionRange|Lock|Trap", re.I)),
]


def classify(path):
    """The class of a GFF field path ("look", "stats" ... see the module docstring), or "other"."""
    for name, rx in CLASSES:
        if rx.search(path):
            return name
    return "other"


def fields_of(db, node):
    """{path: value} of the module file behind a node (bp:x.uti ...).

    db: the analysis index, sqlite3.Connection (only read). The module's own copy is preferred over a hak's
    (s.kind<>'module' sorts False=0 first), then the lowest source priority. Identity fields (IGNORE) are left out.
    Returns None when the node has no file."""
    row = db.execute("SELECT f.id FROM files f JOIN sources s ON s.id=f.source_id WHERE f.node=? "
                     "ORDER BY s.kind<>'module', s.priority LIMIT 1", (node,)).fetchone()
    if not row:
        return None
    return {p: v for p, lab, v in db.execute("SELECT path, label, value FROM fields WHERE file_id=?", (row[0],))
            if lab not in IGNORE}


def compare(db, a, b, limit=500):
    """How blueprint nodes `a` and `b` differ: [dict(path, a, b, cls)] sorted by path, at most `limit` entries.
    A field missing on one side shows as None there. Returns None when either node has no file. Read-only."""
    fa, fb = fields_of(db, a), fields_of(db, b)
    if fa is None or fb is None:
        return None
    out = []
    for p in sorted(set(fa) | set(fb)):
        if fa.get(p) != fb.get(p):
            out.append(dict(path=p, a=fa.get(p), b=fb.get(p), cls=classify(p)))
    return out[:limit]


def summary(diffs, examples=8):
    """Short version of compare()'s list: dict(counts per class, total, examples). The examples are the first
    `examples` differences ordered by class as listed in CLASSES (inventory first), "other" last."""
    counts = {}
    for d in diffs:
        counts[d["cls"]] = counts.get(d["cls"], 0) + 1
    ex = sorted(diffs, key=lambda d: [c for c, _ in CLASSES].index(d["cls"]) if d["cls"] != "other" else 99)[:examples]
    return dict(counts=counts, total=len(diffs), examples=ex)
