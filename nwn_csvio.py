"""
nwn_csvio.py - spreadsheet round-trips: read a CSV file someone edited in Excel or LibreOffice, and write cells a
spreadsheet can't run as formulas.

Why this exists
---------------
Some jobs are quicker in a spreadsheet than on a web page: re-filing a few hundred blueprints, for example. The
dashboard exports a CSV, the builder edits one column, and the page imports it again. Every page that offers that
round-trip reads the file the same way, so the rules live here once:
    * encoding: UTF-8 (with or without the byte-order mark Excel adds to "CSV UTF-8"), else Windows-1252 - what
      Excel writes for plain "CSV (Comma delimited)" on a Western-language Windows;
    * delimiter: comma, semicolon (Excel in countries that write decimals with a comma) or tab, whichever the header
      line uses;
    * header names are compared lower-cased and trimmed; every row gets "_row", its line number as the spreadsheet
      shows it (the header is row 1), so a message can say "row 14".
Cells written by the toolkit go through cell(): text starting with = + - @ (or a tab / carriage return) gets a
leading ', so a spreadsheet shows it instead of evaluating it (OWASP "CSV injection"). uncell() takes that ' off
again when such a file comes back.

Reads only the bytes it is given; writes nothing.
"""
from __future__ import annotations

import csv
import io

FORMULA_START = ("=", "+", "-", "@", "\t", "\r")
MAX_ROWS = 200_000          # far more than any module has blueprints; a bigger file is refused, not half-read


def cell(v):
    """A value for a CSV cell that a spreadsheet can't run: None -> "", text starting with = + - @ (or a tab / CR)
    gets a leading ' (shown, not evaluated). Numbers are returned as they are."""
    if v is None:
        return ""
    if isinstance(v, (int, float)):
        return v
    s = str(v)
    return "'" + s if s[:1] in FORMULA_START else s


def uncell(s):
    """The reverse of cell() for text read back: "'=x" -> "=x". Other text is only trimmed."""
    s = (s or "").strip()
    return s[1:] if len(s) > 1 and s[0] == "'" and s[1] in FORMULA_START else s


def decode(data):
    """Bytes of a CSV file -> text: UTF-8 (a leading byte-order mark is dropped), else Windows-1252. Raises
    ValueError for something that is clearly not text (NUL bytes)."""
    if b"\x00" in data:
        raise ValueError("this is not a CSV text file (it contains binary data) - save it from the spreadsheet as CSV")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def read_upload(data, required=()):
    """Parse an uploaded CSV file into row dicts.

    data: the file's bytes. required: header names (lower case) that must be present. Returns
    [{header: value, ..., "_row": line number}] for every non-empty line after the header; values are strings
    (missing cells ""), header names lower-cased and trimmed. Raises ValueError, naming the problem, for an empty
    file, binary data, a missing required column or more than MAX_ROWS rows. Never writes anything."""
    text = decode(data)
    lines = text.splitlines()
    if not lines or not lines[0].strip():
        raise ValueError("the file is empty - export the CSV from the page, edit it, and import that file")
    head = lines[0]
    # the delimiter the header uses most: Excel's "CSV" is ";" where a comma is the decimal mark
    delim = max((",", ";", "\t"), key=head.count)
    reader = csv.reader(io.StringIO(text), delimiter=delim)
    header = [h.strip().lower() for h in next(reader)]
    missing = [r for r in required if r not in header]
    if missing:
        raise ValueError(f"the file has no {', '.join(missing)} column - its first line must name the columns "
                         f"(found: {', '.join(h for h in header if h) or 'nothing'})")
    rows = []
    for i, rec in enumerate(reader, start=2):
        if not any(c.strip() for c in rec):
            continue
        if len(rows) >= MAX_ROWS:
            raise ValueError(f"the file has more than {MAX_ROWS:,} rows")
        row = {h: (rec[j] if j < len(rec) else "") for j, h in enumerate(header) if h}
        row["_row"] = i
        rows.append(row)
    return rows
