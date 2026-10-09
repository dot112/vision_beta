"""CSV and Excel (.xlsx) files of the product records.

Both read the table in chunks of CHUNK_ROWS, by id (keyset paging), so memory
stays flat however long the range is. The Excel file is written with zipfile
and plain XML (no spreadsheet library): a Records sheet and a Summary sheet.
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import re
import tempfile
import zipfile
from datetime import datetime, timezone, tzinfo
from typing import Any, AsyncIterator, Callable, Dict, Iterable, List, Optional, Tuple
from xml.sax.saxutils import escape

from sqlalchemy import func, literal, select, tuple_

from app.db.models.production_record import ProductRecord
from app.services.product_service import _csv_safe
from app.services.production_records_service import RecordFilter, utc

CHUNK_ROWS = 5000
# Rows an Excel sheet holds, less the header row.
XLSX_MAX_ROWS = 1_048_575

# (header, value of a row). Times are added in front: local, then UTC.
_FIELDS: Tuple[Tuple[str, Callable[[ProductRecord], Any]], ...] = (
    ("line_id", lambda r: r.line_id),
    ("line_name", lambda r: r.line_name),
    ("camera_id", lambda r: r.camera_id),
    ("camera_name", lambda r: r.camera_name),
    ("kind", lambda r: r.kind),
    ("counted", lambda r: bool(r.counted)),
    ("result", lambda r: r.result),
    ("reject_reason", lambda r: r.reject_reason),
    ("class_name", lambda r: r.class_name),
    ("confidence", lambda r: r.confidence),
    ("track_id", lambda r: r.track_id),
    ("code", lambda r: r.code),
    ("code_format", lambda r: r.code_format),
    ("code_status", lambda r: r.code_status),
    ("product_name", lambda r: r.product_name),
    ("product_list_id", lambda r: r.product_list_id),
    ("batch", lambda r: r.batch),
    ("reject_camera_id", lambda r: (r.details or {}).get("reject_camera_id") if isinstance(r.details, dict) else None),
    ("stations", lambda r: _stations(r.details)),
    ("id", lambda r: r.id),
)


def _stations(details: Any) -> Optional[str]:
    stations = details.get("stations") if isinstance(details, dict) else None
    if not stations:
        return None
    return json.dumps(stations, separators=(",", ":"), ensure_ascii=False)


def time_headers(tz_name: str) -> List[str]:
    return [f"time ({tz_name})", "time_utc"]


async def count_rows(db: Any, flt: RecordFilter) -> int:
    return int((await db.execute(flt.apply(select(func.count(ProductRecord.id))))).scalar_one())


async def iter_chunks(session_factory: Callable[[], Any], flt: RecordFilter,
                      chunk: Optional[int] = None) -> AsyncIterator[List[ProductRecord]]:
    """The filtered rows, oldest first, ``chunk`` (CHUNK_ROWS) at a time.

    Keyset paging on (recorded_at, id): each chunk starts in the time index
    where the last one ended, so SQLite neither sorts nor rescans the range
    (paging on id alone makes it sort the whole range again for every chunk
    once a line is picked). Each chunk is read in its own short session, so a
    long download holds no transaction open and the writer never waits on it.
    """
    chunk = chunk or CHUNK_ROWS
    rec = ProductRecord
    last: Optional[Tuple[datetime, int]] = None
    while True:
        stmt = flt.apply(select(rec))
        if last is not None:
            when, last_id = last
            stmt = stmt.where(
                rec.recorded_at >= when,
                tuple_(rec.recorded_at, rec.id) > tuple_(literal(when, rec.recorded_at.type), literal(last_id)),
            )
        async with session_factory() as db:
            rows = list((await db.execute(stmt.order_by(rec.recorded_at, rec.id).limit(chunk))).scalars().all())
        if not rows:
            return
        yield rows
        if len(rows) < chunk:
            return
        last = (rows[-1].recorded_at, rows[-1].id)


def _local(record: ProductRecord, tz: tzinfo) -> Tuple[datetime, datetime]:
    when = utc(record.recorded_at)
    return when.astimezone(tz), when


# ── CSV ───────────────────────────────────────────────────────────────────────

def _csv_cell(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return _csv_safe(value)
    return value


def csv_header(tz_name: str) -> str:
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\r\n").writerow(time_headers(tz_name) + [name for name, _ in _FIELDS])
    # A byte order mark, so spreadsheet programs read the file as UTF-8.
    return "﻿" + buf.getvalue()


def csv_rows(rows: Iterable[ProductRecord], tz: tzinfo) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    for record in rows:
        local, when = _local(record, tz)
        writer.writerow(
            [local.strftime("%Y-%m-%d %H:%M:%S"), when.strftime("%Y-%m-%dT%H:%M:%S.%fZ")]
            + [_csv_cell(get(record)) for _, get in _FIELDS]
        )
    return buf.getvalue()


async def stream_csv(session_factory: Callable[[], Any], flt: RecordFilter, tz: tzinfo, tz_name: str) -> AsyncIterator[bytes]:
    yield csv_header(tz_name).encode("utf-8")
    async for rows in iter_chunks(session_factory, flt):
        yield csv_rows(rows, tz).encode("utf-8")


# ── XLSX ──────────────────────────────────────────────────────────────────────

_EXCEL_EPOCH = datetime(1899, 12, 30)
# Characters XML 1.0 cannot hold; Excel writes them as _xHHHH_.
_XML_BAD = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")
_ESCAPE_LIKE = re.compile(r"_(x[0-9A-Fa-f]{4}_)")


def excel_serial(moment: datetime) -> float:
    """Days since 1899-12-30 (Excel's date number) of a wall-clock time."""
    naive = moment.replace(tzinfo=None)
    return (naive - _EXCEL_EPOCH).total_seconds() / 86400.0


def _xml_text(value: str) -> str:
    value = _ESCAPE_LIKE.sub(r"_x005F_\1", value)
    value = _XML_BAD.sub(lambda m: f"_x{ord(m.group()):04X}_", value)
    return escape(value)


def column_letter(index: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA."""
    letters = ""
    index += 1
    while index:
        index, rest = divmod(index - 1, 26)
        letters = chr(65 + rest) + letters
    return letters


# Cell styles (cellXfs in styles.xml): 0 plain, 1 date and time, 2 bold header, 3 percent.
_DATE_STYLE = 1
_HEADER_STYLE = 2
_PERCENT_STYLE = 3


def _cell(ref: str, value: Any, style: int = 0) -> str:
    s = f' s="{style}"' if style else ""
    if value is None or value == "":
        return f'<c r="{ref}"{s}/>' if style else ""
    if isinstance(value, bool):
        return f'<c r="{ref}" t="b"{s}><v>{1 if value else 0}</v></c>'
    if isinstance(value, datetime):
        return f'<c r="{ref}" s="{_DATE_STYLE}"><v>{excel_serial(value):.10f}</v></c>'
    if isinstance(value, (int, float)):
        return f'<c r="{ref}"{s}><v>{float(value)!r}</v></c>' if isinstance(value, float) else f'<c r="{ref}"{s}><v>{value}</v></c>'
    return f'<c r="{ref}" t="inlineStr"{s}><is><t xml:space="preserve">{_xml_text(str(value))}</t></is></c>'


def sheet_row(number: int, values: Iterable[Any], style: int = 0) -> str:
    cells = "".join(_cell(f"{column_letter(i)}{number}", v, style) for i, v in enumerate(values))
    return f'<row r="{number}">{cells}</row>'


_SHEET_START = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
)

_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
    '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
    '<Override PartName="/xl/worksheets/sheet2.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
    '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
    '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
    '<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>'
    '</Types>'
)

_ROOT_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
    '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>'
    '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>'
    '</Relationships>'
)

_WORKBOOK = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
    '<sheets><sheet name="Records" sheetId="1" r:id="rId1"/><sheet name="Summary" sheetId="2" r:id="rId2"/></sheets>'
    '</workbook>'
)

_WORKBOOK_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
    '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet2.xml"/>'
    '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
    '</Relationships>'
)

_STYLES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
    '<numFmts count="1"><numFmt numFmtId="164" formatCode="yyyy-mm-dd hh:mm:ss"/></numFmts>'
    '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
    '<fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>'
    '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
    '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
    '<cellXfs count="4">'
    '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
    '<xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
    '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/>'
    '<xf numFmtId="10" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
    '</cellXfs>'
    '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
    '</styleSheet>'
)


def _core_props(now: datetime) -> str:
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        '<dc:title>Production records</dc:title><dc:creator>Industrial Vision</dc:creator>'
        f'<dcterms:created xsi:type="dcterms:W3CDTF">{stamp}</dcterms:created>'
        '</cp:coreProperties>'
    )


_APP_PROPS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties">'
    '<Application>Industrial Vision</Application></Properties>'
)


def records_sheet_rows(rows: Iterable[ProductRecord], tz: tzinfo, first_row: int) -> str:
    """The Records sheet's <row> elements for one chunk; ``first_row`` is the first row's number."""
    out = []
    for number, record in enumerate(rows, start=first_row):
        local, when = _local(record, tz)
        out.append(sheet_row(number, [local, when] + [get(record) for _, get in _FIELDS]))
    return "".join(out)


class _Percent(float):
    """A share (0..1) shown as a percentage."""


def _percent(value: Optional[float]) -> Optional[_Percent]:
    return _Percent(value / 100.0) if value is not None else None


def summary_sheet(summary: Dict[str, Any], tz_name: str, line_label: str, start: datetime, end: datetime,
                  tz: tzinfo, note: Optional[str] = None) -> str:
    """The Summary sheet: the range, the totals, and the summary endpoint's tables."""
    rows: List[Tuple[List[Any], int]] = []

    def add(values: List[Any], style: int = 0) -> None:
        rows.append((values, style))

    totals = summary["totals"]
    add(["Production records"], _HEADER_STYLE)
    add(["Line", line_label])
    add([f"From ({tz_name})", start.astimezone(tz)])
    add([f"To ({tz_name})", end.astimezone(tz)])
    if note:
        add(["Note", note])
    add([])
    add(["Total", "Good", "Rejected", "Yield"], _HEADER_STYLE)
    add([totals["total"], totals["good"], totals["reject"], _percent(totals["yield"])])
    add([])
    add(["Class", "Total", "Good", "Rejected"], _HEADER_STYLE)
    for entry in summary["by_class"]:
        add([entry["class_name"] or "(none)", entry["total"], entry["good"], entry["reject"]])
    add([])
    add(["Reject reason", "Rejected"], _HEADER_STYLE)
    for entry in summary["by_reject_reason"]:
        add([entry["reject_reason"] or "(none)", entry["count"]])
    add([])
    add(["Camera", "Camera id", "In line totals", "Total", "Good", "Rejected"], _HEADER_STYLE)
    for entry in summary["by_camera"]:
        add([entry["camera_name"] or entry["camera_id"] or "(none)", entry["camera_id"], entry["counted"],
             entry["total"], entry["good"], entry["reject"]])
    if summary.get("codes"):
        add([])
        add(["Code status", "Rows"], _HEADER_STYLE)
        for status, count in sorted(summary["codes"].items()):
            add([status, count])
    add([])
    add([f"{'Day' if summary['bucket'] == 'day' else 'Hour'} ({tz_name})", "Total", "Good", "Rejected", "Yield"], _HEADER_STYLE)
    for entry in summary["buckets"]:
        start_at = datetime.fromisoformat(entry["start"])
        add([start_at, entry["total"], entry["good"], entry["reject"], _percent(entry["yield"])])

    body = []
    for number, (values, style) in enumerate(rows, start=1):
        cells = "".join(
            _cell(f"{column_letter(i)}{number}", value, _PERCENT_STYLE if isinstance(value, _Percent) else style)
            for i, value in enumerate(values)
        )
        body.append(f'<row r="{number}">{cells}</row>')
    cols = '<cols><col min="1" max="1" width="24" customWidth="1"/><col min="2" max="6" width="14" customWidth="1"/></cols>'
    return _SHEET_START + cols + "<sheetData>" + "".join(body) + "</sheetData></worksheet>"


async def build_xlsx(session_factory: Callable[[], Any], flt: RecordFilter, summary: Dict[str, Any],
                     tz: tzinfo, tz_name: str, line_label: str, note: Optional[str] = None) -> Any:
    """Write the workbook to a spooled temporary file (memory, then disk past 8 MB). Returns it at position 0."""
    spool = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024)
    try:
        with zipfile.ZipFile(spool, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("[Content_Types].xml", _CONTENT_TYPES)
            zf.writestr("_rels/.rels", _ROOT_RELS)
            zf.writestr("docProps/core.xml", _core_props(datetime.now(timezone.utc)))
            zf.writestr("docProps/app.xml", _APP_PROPS)
            zf.writestr("xl/workbook.xml", _WORKBOOK)
            zf.writestr("xl/_rels/workbook.xml.rels", _WORKBOOK_RELS)
            zf.writestr("xl/styles.xml", _STYLES)
            headers = time_headers(tz_name) + [name for name, _ in _FIELDS]
            with zf.open("xl/worksheets/sheet1.xml", "w", force_zip64=True) as sheet:
                sheet.write((
                    _SHEET_START
                    + '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" state="frozen"/></sheetView></sheetViews>'
                    + '<cols><col min="1" max="2" width="20" customWidth="1"/></cols><sheetData>'
                    + sheet_row(1, headers, _HEADER_STYLE)
                ).encode("utf-8"))
                next_row = 2
                async for rows in iter_chunks(session_factory, flt):
                    xml = records_sheet_rows(rows, tz, next_row)
                    next_row += len(rows)
                    # Compressing is the slow part: off the event loop.
                    await asyncio.to_thread(sheet.write, xml.encode("utf-8"))
                sheet.write(b"</sheetData></worksheet>")
            zf.writestr("xl/worksheets/sheet2.xml", summary_sheet(summary, tz_name, line_label, flt.start, flt.end, tz, note))
        spool.seek(0)
        return spool
    except BaseException:
        spool.close()
        raise


async def iter_file(spool: Any, chunk: int = 256 * 1024) -> AsyncIterator[bytes]:
    try:
        while True:
            data = await asyncio.to_thread(spool.read, chunk)
            if not data:
                return
            yield data
    finally:
        spool.close()
