"""
Building the .xlsx export.

Two things here are less obvious than they look, and both are about Excel
being an opinionated program rather than a file format:

1. EXCEL DESTROYS PHONE NUMBERS UNLESS YOU STOP IT.
   Given "+919820012345" in a General-formatted cell, Excel tries to be
   helpful: it sees a long run of digits and reformats it as a number, which
   drops the leading "+" and can render it as 9.2E+11. The E164 numbers we
   worked so hard to produce in postprocess.py would arrive in the user's
   spreadsheet mangled. Forcing the cell's number_format to "@" (text) tells
   Excel to leave the characters exactly as written. Same reasoning applies to
   any ID-like column.

2. THE FILE IS BUILT IN MEMORY, NEVER ON DISK.
   openpyxl can save to a file path, but a web server writing temp files has
   to invent unique names, clean them up, and handle two requests racing for
   the same path. Saving into a BytesIO avoids all three problems: the bytes
   go straight into the HTTP response and are garbage-collected afterwards.
   A workbook of a few thousand leads is well under a megabyte.
"""

import io
import re
from datetime import datetime, timezone

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from app.schema import LEAD_FIELDS, Lead

# Column order in the sheet: the seven extracted fields first, then provenance.
# Diagnostics go last so the left-hand side of the sheet is the part a
# salesperson actually wants, and the audit trail is there without being in
# the way.
EXPORT_COLUMNS: tuple[tuple[str, str], ...] = (
    ("first_name", "First name"),
    ("last_name", "Last name"),
    ("title", "Title"),
    ("company", "Company"),
    ("location", "Location"),
    ("phone", "Phone"),
    ("email", "Email"),
    ("source_filename", "Source file"),
    ("status", "Status"),
    ("error", "Problem"),
)

# Columns Excel must not reinterpret. Phone is the critical one.
TEXT_COLUMNS = {"phone"}

HEADER_FILL = PatternFill("solid", fgColor="14202E")   # ink
HEADER_FONT = Font(color="FFFFFF", bold=True)
FLAGGED_FILL = PatternFill("solid", fgColor="FDF2E3")  # pale amber
FAILED_FILL = PatternFill("solid", fgColor="FBE9EC")   # pale rose

# Sensible starting widths, in Excel's character units.
COLUMN_WIDTHS = {
    "first_name": 14, "last_name": 14, "title": 26, "company": 30,
    "location": 20, "phone": 18, "email": 32,
    "source_filename": 22, "status": 13, "error": 52,
}


def _safe_sheet_title(raw: str) -> str:
    """
    Excel sheet names cannot contain : \\ / ? * [ ] and cap at 31 characters.
    Violating either makes the file unopenable, so we sanitise rather than
    trust a caller-supplied string.
    """
    cleaned = re.sub(r"[:\\/?*\[\]]", "-", raw).strip() or "Leads"
    return cleaned[:31]


def build_workbook(leads: list[Lead], sheet_title: str = "Leads") -> bytes:
    """Render leads to .xlsx and return the raw bytes."""
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = _safe_sheet_title(sheet_title)

    # --- header row ------------------------------------------------------
    for index, (_field, label) in enumerate(EXPORT_COLUMNS, start=1):
        cell = sheet.cell(row=1, column=index, value=label)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(vertical="center")

    # --- data rows -------------------------------------------------------
    for row_number, lead in enumerate(leads, start=2):
        # Tint the whole row when the card did not extract cleanly, so a human
        # scanning the sheet can see at a glance which cards need re-shooting.
        fill = None
        if lead.status == "model_error":
            fill = FAILED_FILL
        elif lead.status != "ok":
            fill = FLAGGED_FILL

        for index, (field, _label) in enumerate(EXPORT_COLUMNS, start=1):
            value = getattr(lead, field, None)
            cell = sheet.cell(row=row_number, column=index, value=value)
            if field in TEXT_COLUMNS:
                # "@" means "treat this as text" -- see the module docstring.
                cell.number_format = "@"
            if fill is not None:
                cell.fill = fill

    # --- make the sheet usable -------------------------------------------
    for index, (field, _label) in enumerate(EXPORT_COLUMNS, start=1):
        sheet.column_dimensions[get_column_letter(index)].width = COLUMN_WIDTHS[field]

    # Freeze below the header so it stays visible while scrolling, and add an
    # autofilter so the user can immediately filter to status = ok. Both are
    # one line each and are the difference between "a data dump" and "a
    # spreadsheet someone can work in".
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = (
        f"A1:{get_column_letter(len(EXPORT_COLUMNS))}{max(len(leads) + 1, 2)}"
    )

    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def export_filename(prefix: str = "leads") -> str:
    """A timestamped filename, so repeated downloads do not overwrite."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    return f"{prefix}-{stamp}.xlsx"
