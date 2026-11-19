import csv
import re
from io import BytesIO, StringIO

from docx import Document
from docx.enum.section import WD_ORIENT
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT, WD_TABLE_ALIGNMENT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor
from pptx import Presentation
from pptx.dml.color import RGBColor as PptxRGBColor
from pptx.enum.text import MSO_ANCHOR
from pptx.util import Inches as PptxInches
from pptx.util import Pt as PptxPt


_CELL_RE = re.compile(r"^\$?([A-Z]+)\$?([1-9]\d*)$", re.IGNORECASE)


def _column_index(value: str) -> int:
    index = 0
    for char in value.upper():
        index = index * 26 + ord(char) - ord("A") + 1
    return index - 1


def parse_a1_range(range_name: str) -> tuple[str, int, int, int, int]:
    sheet_name = ""
    coordinates = range_name.strip()
    if "!" in coordinates:
        sheet_name, coordinates = coordinates.rsplit("!", 1)
        sheet_name = sheet_name.strip()
        if sheet_name.startswith("'") and sheet_name.endswith("'"):
            sheet_name = sheet_name[1:-1].replace("''", "'")

    cells = coordinates.split(":")
    if len(cells) > 2:
        raise ValueError("Use a valid A1 range such as Sheet1!A1:D20.")
    first = _CELL_RE.fullmatch(cells[0].strip())
    last = _CELL_RE.fullmatch(cells[-1].strip())
    if not first or not last:
        raise ValueError("Use a valid A1 range such as Sheet1!A1:D20.")

    start_column = _column_index(first.group(1))
    start_row = int(first.group(2)) - 1
    end_column = _column_index(last.group(1))
    end_row = int(last.group(2)) - 1
    if end_row < start_row or end_column < start_column:
        raise ValueError("The end of the A1 range must not precede its start.")
    return sheet_name, start_row, end_row + 1, start_column, end_column + 1


def grid_range(range_name: str, sheet_id: int) -> tuple[str, dict]:
    sheet_name, start_row, end_row, start_column, end_column = parse_a1_range(range_name)
    return sheet_name, {
        "sheetId": sheet_id,
        "startRowIndex": start_row,
        "endRowIndex": end_row,
        "startColumnIndex": start_column,
        "endColumnIndex": end_column,
    }


def quoted_sheet_range(sheet_name: str, a1_range: str = "A1:Z50") -> str:
    escaped_name = sheet_name.replace("'", "''")
    return f"'{escaped_name}'!{a1_range}"


def professional_format_requests(sheet_id: int, column_count: int, header_row: int = 1) -> list[dict]:
    header_index = max(header_row - 1, 0)
    requests = [
        {
            "updateSheetProperties": {
                "properties": {
                    "sheetId": sheet_id,
                    "gridProperties": {"frozenRowCount": header_row},
                },
                "fields": "gridProperties.frozenRowCount",
            }
        },
        {
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": header_index,
                    "endRowIndex": header_index + 1,
                    "startColumnIndex": 0,
                    "endColumnIndex": max(column_count, 1),
                },
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": {"red": 0.12, "green": 0.25, "blue": 0.40},
                        "textFormat": {
                            "foregroundColor": {"red": 1, "green": 1, "blue": 1},
                            "bold": True,
                        },
                        "verticalAlignment": "MIDDLE",
                        "wrapStrategy": "WRAP",
                    }
                },
                "fields": "userEnteredFormat(backgroundColor,textFormat,verticalAlignment,wrapStrategy)",
            }
        },
        {
            "autoResizeDimensions": {
                "dimensions": {
                    "sheetId": sheet_id,
                    "dimension": "COLUMNS",
                    "startIndex": 0,
                    "endIndex": max(column_count, 1),
                }
            }
        },
    ]
    return requests


def csv_export(rows: list[list[object]]) -> bytes:
    output = StringIO(newline="")
    writer = csv.writer(output, lineterminator="\r\n")
    writer.writerows(rows)
    return output.getvalue().encode("utf-8-sig")


def _cell_text(value: object, max_chars: int = 240) -> tuple[str, bool]:
    text = "" if value is None else str(value)
    if len(text) > max_chars:
        return text[:max_chars - 3] + "...", True
    return text, False


def _set_docx_cell_fill(cell, color: str) -> None:
    shading = OxmlElement("w:shd")
    shading.set(qn("w:fill"), color)
    cell._tc.get_or_add_tcPr().append(shading)


def _docx_export(title: str, sheet_name: str, rows: list[list[object]]) -> tuple[bytes, bool]:
    max_rows, max_columns = 500, 16
    truncated = (
        len(rows) > max_rows
        or max((len(row) for row in rows), default=0) > max_columns
        or any(len(str(value or "")) > 240 for row in rows[:max_rows] for value in row[:max_columns])
    )
    values = [row[:max_columns] for row in rows[:max_rows]]
    column_count = max((len(row) for row in values), default=1)

    document = Document()
    section = document.sections[0]
    section.orientation = WD_ORIENT.LANDSCAPE
    section.page_width, section.page_height = section.page_height, section.page_width
    section.left_margin = section.right_margin = Inches(0.55)
    section.top_margin = section.bottom_margin = Inches(0.55)

    heading = document.add_heading(title, level=0)
    heading.runs[0].font.color.rgb = RGBColor(31, 78, 121)
    subtitle = document.add_paragraph(sheet_name)
    subtitle.runs[0].font.color.rgb = RGBColor(100, 116, 139)
    if truncated:
        note = document.add_paragraph("This export is limited to the first 500 rows, 16 columns, and 240 characters per cell.")
        note.runs[0].italic = True
        note.runs[0].font.size = Pt(8)

    if values:
        table = document.add_table(rows=len(values), cols=column_count)
        table.style = "Table Grid"
        table.alignment = WD_TABLE_ALIGNMENT.CENTER
        for row_index, row in enumerate(values):
            for column_index, value in enumerate(row):
                cell = table.cell(row_index, column_index)
                cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
                text, shortened = _cell_text(value, 240)
                truncated = truncated or shortened
                cell.text = text
                for paragraph in cell.paragraphs:
                    for run in paragraph.runs:
                        run.font.name = "Aptos"
                        run.font.size = Pt(8)
                        if row_index == 0:
                            run.font.bold = True
                            run.font.color.rgb = RGBColor(255, 255, 255)
                if row_index == 0:
                    _set_docx_cell_fill(cell, "1F4E79")
        header_properties = OxmlElement("w:tblHeader")
        header_properties.set(qn("w:val"), "true")
        table.rows[0]._tr.get_or_add_trPr().append(header_properties)
    else:
        document.add_paragraph("This sheet has no data.")

    output = BytesIO()
    document.save(output)
    return output.getvalue(), truncated


def _pptx_export(title: str, sheet_name: str, rows: list[list[object]]) -> tuple[bytes, bool]:
    max_rows, max_columns = 200, 12
    values = [row[:max_columns] for row in rows[:max_rows]]
    truncated = len(rows) > max_rows or max((len(row) for row in rows), default=0) > max_columns
    truncated = truncated or any(len(str(value or "")) > 240 for row in values for value in row)

    presentation = Presentation()
    presentation.slide_width = PptxInches(13.333)
    presentation.slide_height = PptxInches(7.5)

    title_slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    title_shape = title_slide.shapes.add_textbox(PptxInches(0.8), PptxInches(2.2), PptxInches(11.7), PptxInches(1.0))
    title_frame = title_shape.text_frame
    title_frame.text = title
    title_frame.paragraphs[0].font.size = PptxPt(34)
    title_frame.paragraphs[0].font.bold = True
    title_frame.paragraphs[0].font.color.rgb = PptxRGBColor(31, 78, 121)
    subtitle_shape = title_slide.shapes.add_textbox(PptxInches(0.85), PptxInches(3.35), PptxInches(11.5), PptxInches(0.55))
    subtitle_shape.text_frame.text = sheet_name
    subtitle_shape.text_frame.paragraphs[0].font.size = PptxPt(18)
    subtitle_shape.text_frame.paragraphs[0].font.color.rgb = PptxRGBColor(100, 116, 139)

    if values:
        column_count = max((len(row) for row in values), default=1)
        header = values[0]
        data_rows = values[1:] if len(values) > 1 else []
        row_chunks = [data_rows[index:index + 12] for index in range(0, len(data_rows), 12)] or [[]]
        column_ranges = [(start, min(start + 6, column_count)) for start in range(0, column_count, 6)]
        for row_index, row_chunk in enumerate(row_chunks):
            for column_start, column_end in column_ranges:
                slide = presentation.slides.add_slide(presentation.slide_layouts[6])
                slide_title = slide.shapes.add_textbox(PptxInches(0.45), PptxInches(0.2), PptxInches(12.3), PptxInches(0.55))
                slide_title.text_frame.text = f"{title} · {sheet_name}"
                slide_title.text_frame.paragraphs[0].font.size = PptxPt(20)
                slide_title.text_frame.paragraphs[0].font.bold = True
                slide_title.text_frame.paragraphs[0].font.color.rgb = PptxRGBColor(31, 78, 121)

                visible_header = header[column_start:column_end]
                table = slide.shapes.add_table(
                    len(row_chunk) + 1,
                    max(len(visible_header), 1),
                    PptxInches(0.45),
                    PptxInches(1.05),
                    PptxInches(12.3),
                    PptxInches(5.8),
                ).table
                table.rows[0].height = PptxInches(0.45)
                for column_index, value in enumerate(visible_header):
                    _style_pptx_cell(table.cell(0, column_index), value, is_header=True, font_size=12)
                for local_row_index, row in enumerate(row_chunk, start=1):
                    table.rows[local_row_index].height = PptxInches(0.42)
                    for column_index, value in enumerate(row[column_start:column_end]):
                        _style_pptx_cell(
                            table.cell(local_row_index, column_index),
                            value,
                            is_header=False,
                            font_size=10,
                            fill_color=(245, 248, 252) if local_row_index % 2 else (255, 255, 255),
                        )

                page_note = slide.shapes.add_textbox(PptxInches(0.45), PptxInches(6.95), PptxInches(12.3), PptxInches(0.25))
                page_note.text_frame.text = f"Rows {row_index * 12 + 1}–{row_index * 12 + len(row_chunk)} · Columns {column_start + 1}–{column_end}"
                page_note.text_frame.paragraphs[0].font.size = PptxPt(9)
                page_note.text_frame.paragraphs[0].font.color.rgb = PptxRGBColor(100, 116, 139)
    else:
        empty_slide = presentation.slides.add_slide(presentation.slide_layouts[6])
        message = empty_slide.shapes.add_textbox(PptxInches(0.8), PptxInches(2.8), PptxInches(11.5), PptxInches(0.6))
        message.text_frame.text = "This sheet has no data."

    if truncated:
        note_slide = presentation.slides.add_slide(presentation.slide_layouts[6])
        note = note_slide.shapes.add_textbox(PptxInches(0.8), PptxInches(2.8), PptxInches(11.5), PptxInches(0.8))
        note.text_frame.text = "This presentation is limited to the first 200 data rows, 12 columns, and 240 characters per cell."
        note.text_frame.paragraphs[0].font.size = PptxPt(14)

    output = BytesIO()
    presentation.save(output)
    return output.getvalue(), truncated


def _style_pptx_cell(cell, value: object, is_header: bool, font_size: int, fill_color: tuple[int, int, int] = (255, 255, 255)) -> None:
    cell.text = _cell_text(value)[0]
    cell.vertical_anchor = MSO_ANCHOR.MIDDLE
    cell.fill.solid()
    cell.fill.fore_color.rgb = PptxRGBColor(*(31, 78, 121) if is_header else fill_color)
    for paragraph in cell.text_frame.paragraphs:
        for run in paragraph.runs:
            run.font.size = PptxPt(font_size)
            run.font.bold = is_header
            run.font.color.rgb = PptxRGBColor(255, 255, 255) if is_header else PptxRGBColor(40, 50, 60)


def office_export(format_name: str, title: str, sheet_name: str, rows: list[list[object]]) -> tuple[bytes, str, bool]:
    normalized = format_name.upper()
    if normalized == "DOCX":
        content, truncated = _docx_export(title, sheet_name, rows)
        return content, "application/vnd.openxmlformats-officedocument.wordprocessingml.document", truncated
    if normalized == "PPTX":
        content, truncated = _pptx_export(title, sheet_name, rows)
        return content, "application/vnd.openxmlformats-officedocument.presentationml.presentation", truncated
    raise ValueError("Office export format must be DOCX or PPTX.")


def sanitize_download_filename(value: str, extension: str) -> str:
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip()).strip("._-") or "spreadsheet"
    return f"{base[:100]}.{extension}"
