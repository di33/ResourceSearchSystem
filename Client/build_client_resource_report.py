from __future__ import annotations

import datetime as dt
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from resource_contracts.resource_types import (  # noqa: E402
    ANIMATION_SEQUENCE_RESOURCE_TYPE,
    ATLAS_RESOURCE_TYPE,
    AUDIO_FILE_RESOURCE_TYPE,
    FONT_FILE_RESOURCE_TYPE,
    PACK_RESOURCE_TYPE,
    RESOURCE_TYPE_DISPLAY_NAMES_ZH,
    SINGLE_IMAGE_RESOURCE_TYPE,
    TILED_MAP_RESOURCE_TYPE,
    TILED_TILESET_RESOURCE_TYPE,
    TILESET_RESOURCE_TYPE,
)

DB_PATH = REPO_ROOT / "data" / "databases" / "pipeline.db"
OUT_PATH = REPO_ROOT / "data" / "reports" / "client_resource_report.docx"
CLIENT_DB_CANDIDATES = [
    REPO_ROOT / "client" / "pipeline.db",
    REPO_ROOT / "client" / "Scripts" / "ResourceProcessor" / "pipeline.db",
    REPO_ROOT / "data" / "databases" / "pipeline.db",
]

DISPLAY_NAMES = dict(RESOURCE_TYPE_DISPLAY_NAMES_ZH)

STRUCTURE_NOTES = {
    SINGLE_IMAGE_RESOURCE_TYPE: (
        "独立图片资源。通常是一条 resource_task 对应一个主文件；少量记录可能附带同格式文件。"
        "格式来自 resource_file.file_format，主文件通过 is_primary=1 标识。"
    ),
    AUDIO_FILE_RESOURCE_TYPE: (
        "独立音频资源。结构以单个音频文件为主，预览通常是 metadata card，描述依赖标题、包名和路径上下文。"
    ),
    ANIMATION_SEQUENCE_RESOURCE_TYPE: (
        "多帧动画资源。一个任务下挂多张帧图，file_role 多为 frame/main，预览策略倾向于抽帧 GIF。"
    ),
    TILESET_RESOURCE_TYPE: (
        "图块集合资源。一个任务包含多张 tile 图片，常见 file_role 为 tile/main，预览为 contact_sheet 拼贴。"
    ),
    PACK_RESOURCE_TYPE: (
        "包级聚合资源。resource_path 常为 __pack__，任务下可挂大量主文件与附件，"
        "用于表示一个完整素材包或项目包。"
    ),
    ATLAS_RESOURCE_TYPE: (
        "图集资源。通常由图像和描述/索引文件组合，如 PNG + XML；主文件可能是图片或索引文件。"
    ),
    TILED_MAP_RESOURCE_TYPE: (
        "Tiled 地图资源。以 TMX 地图文件为核心，并关联地图引用的 PNG 素材。"
    ),
    FONT_FILE_RESOURCE_TYPE: (
        "字体资源。单个 TTF/OTF 文件为主，预览通常是说明型卡片或静态预览。"
    ),
    TILED_TILESET_RESOURCE_TYPE: (
        "Tiled 图块定义资源。以 TSX 定义文件为核心，记录图块集元数据。"
    ),
}


def rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def one(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> dict | None:
    row = conn.execute(sql, params).fetchone()
    return dict(row) if row else None


def fmt_int(value: int | None) -> str:
    return f"{int(value or 0):,}"


def fmt_pct(count: int, total: int) -> str:
    return f"{count / total * 100:.2f}%" if total else "0.00%"


def compact_text(value: object, limit: int = 90) -> str:
    text = "" if value is None else str(value)
    text = " ".join(text.replace("\r", " ").replace("\n", " ").split())
    if len(text) > limit:
        return text[: limit - 3] + "..."
    return text


def type_label(resource_type: str) -> str:
    name = DISPLAY_NAMES.get(resource_type, resource_type)
    return f"{name} ({resource_type})"


def format_summary(items: list[dict], count_key: str = "file_count", limit: int = 7) -> str:
    if not items:
        return "-"
    shown = []
    for item in items[:limit]:
        shown.append(f"{item['file_format']} {fmt_int(item[count_key])}")
    rest = len(items) - limit
    if rest > 0:
        shown.append(f"另 {rest} 种")
    return "; ".join(shown)


def collect_data() -> dict:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row

    data: dict = {}
    data["db_path"] = str(DB_PATH.resolve())
    data["db_size"] = DB_PATH.stat().st_size if DB_PATH.exists() else 0
    data["client_db_candidates"] = [
        {
            "path": str(path),
            "exists": path.exists(),
            "size": path.stat().st_size if path.exists() else None,
        }
        for path in CLIENT_DB_CANDIDATES
    ]

    data["table_counts"] = rows(
        conn,
        """
        SELECT name AS table_name,
               (SELECT COUNT(*) FROM sqlite_master) AS _dummy
        FROM sqlite_master
        WHERE type='table'
        ORDER BY name
        """,
    )
    for item in data["table_counts"]:
        item["row_count"] = one(conn, f"SELECT COUNT(*) AS c FROM {item['table_name']}")["c"]
        item.pop("_dummy", None)

    data["resource_type_counts"] = rows(
        conn,
        """
        SELECT resource_type, COUNT(*) AS resource_count
        FROM resource_task
        GROUP BY resource_type
        ORDER BY resource_count DESC, resource_type
        """,
    )
    data["total_resources"] = sum(row["resource_count"] for row in data["resource_type_counts"])

    data["state_counts"] = rows(
        conn,
        """
        SELECT process_state, COUNT(*) AS resource_count
        FROM resource_task
        GROUP BY process_state
        ORDER BY resource_count DESC, process_state
        """,
    )
    data["state_by_type"] = rows(
        conn,
        """
        SELECT resource_type, process_state, COUNT(*) AS resource_count
        FROM resource_task
        GROUP BY resource_type, process_state
        ORDER BY resource_type, process_state
        """,
    )
    data["file_count_stats"] = rows(
        conn,
        """
        WITH c AS (
          SELECT rt.id, rt.resource_type, COUNT(rf.id) AS file_count
          FROM resource_task rt
          LEFT JOIN resource_file rf ON rf.task_id = rt.id
          GROUP BY rt.id, rt.resource_type
        )
        SELECT resource_type,
               COUNT(*) AS resource_count,
               MIN(file_count) AS min_files,
               MAX(file_count) AS max_files,
               ROUND(AVG(file_count), 2) AS avg_files,
               SUM(CASE WHEN file_count=0 THEN 1 ELSE 0 END) AS zero_file_resources,
               SUM(CASE WHEN file_count=1 THEN 1 ELSE 0 END) AS single_file_resources,
               SUM(CASE WHEN file_count>1 THEN 1 ELSE 0 END) AS multi_file_resources
        FROM c
        GROUP BY resource_type
        ORDER BY resource_count DESC, resource_type
        """,
    )
    data["parent_stats"] = rows(
        conn,
        """
        SELECT resource_type,
               SUM(CASE WHEN parent_resource_id IS NOT NULL AND parent_resource_id <> '' THEN 1 ELSE 0 END) AS child_resources,
               SUM(CASE WHEN parent_resource_id IS NULL OR parent_resource_id = '' THEN 1 ELSE 0 END) AS root_resources
        FROM resource_task
        GROUP BY resource_type
        ORDER BY resource_type
        """,
    )
    data["file_format_counts"] = rows(
        conn,
        """
        SELECT rt.resource_type, lower(rf.file_format) AS file_format,
               COUNT(*) AS file_count,
               COUNT(DISTINCT rf.task_id) AS resource_count
        FROM resource_file rf
        JOIN resource_task rt ON rt.id = rf.task_id
        GROUP BY rt.resource_type, lower(rf.file_format)
        ORDER BY rt.resource_type, file_count DESC, file_format
        """,
    )
    data["primary_file_format_counts"] = rows(
        conn,
        """
        SELECT rt.resource_type, lower(rf.file_format) AS file_format,
               COUNT(*) AS primary_file_count,
               COUNT(DISTINCT rf.task_id) AS resource_count
        FROM resource_file rf
        JOIN resource_task rt ON rt.id = rf.task_id
        WHERE rf.is_primary = 1
        GROUP BY rt.resource_type, lower(rf.file_format)
        ORDER BY rt.resource_type, primary_file_count DESC, file_format
        """,
    )
    data["file_role_counts"] = rows(
        conn,
        """
        SELECT rt.resource_type, rf.file_role,
               COUNT(*) AS file_count,
               COUNT(DISTINCT rf.task_id) AS resource_count
        FROM resource_file rf
        JOIN resource_task rt ON rt.id = rf.task_id
        GROUP BY rt.resource_type, rf.file_role
        ORDER BY rt.resource_type, file_count DESC, rf.file_role
        """,
    )
    data["preview_counts"] = rows(
        conn,
        """
        SELECT rt.resource_type, rp.strategy, rp.role,
               lower(COALESCE(rp.format,'')) AS preview_format,
               rp.used_placeholder,
               COUNT(*) AS preview_count,
               COUNT(DISTINCT rp.task_id) AS resource_count
        FROM resource_preview rp
        JOIN resource_task rt ON rt.id = rp.task_id
        GROUP BY rt.resource_type, rp.strategy, rp.role,
                 lower(COALESCE(rp.format,'')), rp.used_placeholder
        ORDER BY rt.resource_type, preview_count DESC
        """,
    )
    data["description_counts"] = rows(
        conn,
        """
        SELECT rt.resource_type,
               COUNT(rd.id) AS description_rows,
               COUNT(DISTINCT rd.task_id) AS resources_with_description,
               ROUND(AVG(rd.quality_score), 2) AS avg_quality_score
        FROM resource_task rt
        LEFT JOIN resource_description rd ON rd.task_id = rt.id
        GROUP BY rt.resource_type
        ORDER BY rt.resource_type
        """,
    )
    data["overall_description"] = one(
        conn,
        """
        SELECT COUNT(*) AS description_rows,
               COUNT(DISTINCT task_id) AS resources_with_description
        FROM resource_description
        """,
    )

    latest_desc = """
        SELECT d.task_id, d.main_content
        FROM resource_description d
        JOIN (
            SELECT task_id, MAX(id) AS max_id
            FROM resource_description
            GROUP BY task_id
        ) last ON last.task_id = d.task_id AND last.max_id = d.id
    """

    data["examples_by_type"] = {}
    for type_row in data["resource_type_counts"]:
        typ = type_row["resource_type"]
        data["examples_by_type"][typ] = rows(
            conn,
            f"""
            SELECT rt.id, rt.resource_type, rt.source, rt.source_resource_id, rt.title,
                   rt.pack_name, rt.resource_path, rt.parent_resource_id,
                   rt.process_state, rt.resource_id,
                   COUNT(DISTINCT rf.id) AS file_count,
                   GROUP_CONCAT(DISTINCT lower(rf.file_format)) AS formats,
                   GROUP_CONCAT(DISTINCT rf.file_role) AS roles,
                   MAX(CASE WHEN rf.is_primary=1 THEN rf.file_name ELSE NULL END) AS primary_file,
                   COUNT(DISTINCT rp.id) AS preview_count,
                   GROUP_CONCAT(DISTINCT rp.strategy) AS preview_strategies,
                   MAX(ld.main_content) AS description_main
            FROM resource_task rt
            LEFT JOIN resource_file rf ON rf.task_id = rt.id
            LEFT JOIN resource_preview rp ON rp.task_id = rt.id
            LEFT JOIN ({latest_desc}) ld ON ld.task_id = rt.id
            WHERE rt.resource_type = ?
            GROUP BY rt.id
            ORDER BY file_count DESC, preview_count DESC, rt.id ASC
            LIMIT 3
            """,
            (typ,),
        )

    single_formats = rows(
        conn,
        """
        SELECT lower(rf.file_format) AS file_format, COUNT(*) AS c
        FROM resource_file rf
        JOIN resource_task rt ON rt.id = rf.task_id
        WHERE rt.resource_type=? AND rf.is_primary=1
        GROUP BY lower(rf.file_format)
        ORDER BY c DESC, file_format
        """,
        (SINGLE_IMAGE_RESOURCE_TYPE,),
    )
    data["single_image_examples_by_format"] = {}
    for fmt in [row["file_format"] for row in single_formats]:
        data["single_image_examples_by_format"][fmt] = rows(
            conn,
            f"""
            SELECT rt.id, rt.source, rt.source_resource_id, rt.title, rt.pack_name,
                   rt.resource_path, rt.process_state,
                   rf.file_name, rf.file_path, rf.file_size,
                   lower(rf.file_format) AS file_format,
                   rp.strategy AS preview_strategy, rp.format AS preview_format,
                   rp.width, rp.height,
                   ld.main_content AS description_main
            FROM resource_task rt
            JOIN resource_file rf ON rf.task_id = rt.id AND rf.is_primary=1
            LEFT JOIN resource_preview rp ON rp.task_id = rt.id AND rp.role='primary'
            LEFT JOIN ({latest_desc}) ld ON ld.task_id = rt.id
            WHERE rt.resource_type=? AND lower(rf.file_format)=?
            GROUP BY rt.id
            ORDER BY rt.id ASC
            LIMIT 3
            """,
            (SINGLE_IMAGE_RESOURCE_TYPE, fmt),
        )

    conn.close()
    return data


class Docx:
    def __init__(self) -> None:
        self.body: list[str] = []

    def r(self, text: object, *, bold: bool = False, color: str | None = None, size: int | None = None) -> str:
        text = "" if text is None else str(text)
        props = []
        if bold:
            props.append("<w:b/>")
        if color:
            props.append(f'<w:color w:val="{color}"/>')
        if size:
            props.append(f'<w:sz w:val="{size}"/><w:szCs w:val="{size}"/>')
        props.append('<w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:eastAsia="Microsoft YaHei"/>')
        rpr = f"<w:rPr>{''.join(props)}</w:rPr>"
        return f'<w:r>{rpr}<w:t xml:space="preserve">{escape(text)}</w:t></w:r>'

    def p_xml(
        self,
        text: object = "",
        *,
        style: str = "Normal",
        bold: bool = False,
        color: str | None = None,
        size: int | None = None,
        before: int | None = None,
        after: int | None = None,
        page_break_before: bool = False,
        keep_next: bool = False,
    ) -> str:
        ppr_parts = [f'<w:pStyle w:val="{style}"/>']
        if before is not None or after is not None:
            ppr_parts.append(
                f'<w:spacing w:before="{before if before is not None else 0}" '
                f'w:after="{after if after is not None else 0}" w:line="300" w:lineRule="auto"/>'
            )
        if page_break_before:
            ppr_parts.append("<w:pageBreakBefore/>")
        if keep_next:
            ppr_parts.append("<w:keepNext/>")
        return f"<w:p><w:pPr>{''.join(ppr_parts)}</w:pPr>{self.r(text, bold=bold, color=color, size=size)}</w:p>"

    def p(self, text: object = "", **kwargs) -> None:
        self.body.append(self.p_xml(text, **kwargs))

    def heading(self, text: str, level: int = 1, *, page_break_before: bool = False) -> None:
        style = f"Heading{level}"
        self.body.append(self.p_xml(text, style=style, page_break_before=page_break_before, keep_next=True))

    def table(self, headers: list[str], rows_: list[list[object]], widths: list[int]) -> None:
        assert sum(widths) == 9360, f"table widths must sum to 9360, got {sum(widths)}"
        grid = "".join(f'<w:gridCol w:w="{w}"/>' for w in widths)
        xml = [
            "<w:tbl>",
            "<w:tblPr>",
            '<w:tblW w:w="9360" w:type="dxa"/>',
            '<w:tblInd w:w="120" w:type="dxa"/>',
            '<w:tblLayout w:type="fixed"/>',
            "<w:tblBorders>",
            '<w:top w:val="single" w:sz="4" w:space="0" w:color="B8C2CC"/>',
            '<w:left w:val="single" w:sz="4" w:space="0" w:color="B8C2CC"/>',
            '<w:bottom w:val="single" w:sz="4" w:space="0" w:color="B8C2CC"/>',
            '<w:right w:val="single" w:sz="4" w:space="0" w:color="B8C2CC"/>',
            '<w:insideH w:val="single" w:sz="4" w:space="0" w:color="D7DEE8"/>',
            '<w:insideV w:val="single" w:sz="4" w:space="0" w:color="D7DEE8"/>',
            "</w:tblBorders>",
            "<w:tblCellMar>",
            '<w:top w:w="80" w:type="dxa"/>',
            '<w:left w:w="120" w:type="dxa"/>',
            '<w:bottom w:w="80" w:type="dxa"/>',
            '<w:right w:w="120" w:type="dxa"/>',
            "</w:tblCellMar>",
            "</w:tblPr>",
            f"<w:tblGrid>{grid}</w:tblGrid>",
        ]

        def cell(value: object, width: int, header: bool = False) -> str:
            fill = '<w:shd w:fill="E8EEF5"/>' if header else ""
            style = "TableHeader" if header else "TableText"
            text = compact_text(value, 220)
            return (
                "<w:tc>"
                f'<w:tcPr><w:tcW w:w="{width}" w:type="dxa"/><w:vAlign w:val="center"/>{fill}</w:tcPr>'
                f'{self.p_xml(text, style=style)}'
                "</w:tc>"
            )

        xml.append("<w:tr>" + "".join(cell(h, w, True) for h, w in zip(headers, widths)) + "</w:tr>")
        for row in rows_:
            padded = list(row) + [""] * (len(headers) - len(row))
            xml.append("<w:tr>" + "".join(cell(v, w, False) for v, w in zip(padded, widths)) + "</w:tr>")
        xml.append("</w:tbl>")
        self.body.append("".join(xml))
        self.p("", after=80)

    def document_xml(self) -> str:
        sect = (
            "<w:sectPr>"
            '<w:pgSz w:w="12240" w:h="15840"/>'
            '<w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440" '
            'w:header="708" w:footer="708" w:gutter="0"/>'
            '<w:cols w:space="720"/>'
            '<w:docGrid w:linePitch="360"/>'
            "</w:sectPr>"
        )
        return (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<w:document xmlns:wpc="http://schemas.microsoft.com/office/word/2010/wordprocessingCanvas" '
            'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
            'xmlns:o="urn:schemas-microsoft-com:office:office" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
            'xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math" '
            'xmlns:v="urn:schemas-microsoft-com:vml" '
            'xmlns:wp14="http://schemas.microsoft.com/office/word/2010/wordprocessingDrawing" '
            'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
            'xmlns:w10="urn:schemas-microsoft-com:office:word" '
            'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
            'xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml" '
            'xmlns:wpg="http://schemas.microsoft.com/office/word/2010/wordprocessingGroup" '
            'xmlns:wpi="http://schemas.microsoft.com/office/word/2010/wordprocessingInk" '
            'xmlns:wne="http://schemas.microsoft.com/office/word/2006/wordml" '
            'xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape" '
            'mc:Ignorable="w14 wp14">'
            f"<w:body>{''.join(self.body)}{sect}</w:body></w:document>"
        )


def styles_xml() -> str:
    def style(style_id: str, name: str, size: int, color: str, before: int, after: int, bold: bool = False) -> str:
        b = "<w:b/>" if bold else ""
        return (
            f'<w:style w:type="paragraph" w:styleId="{style_id}">'
            f'<w:name w:val="{name}"/>'
            '<w:qFormat/>'
            f'<w:pPr><w:spacing w:before="{before}" w:after="{after}" w:line="300" w:lineRule="auto"/></w:pPr>'
            '<w:rPr>'
            f'<w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:eastAsia="Microsoft YaHei"/>'
            f"{b}"
            f'<w:color w:val="{color}"/>'
            f'<w:sz w:val="{size}"/><w:szCs w:val="{size}"/>'
            '</w:rPr>'
            '</w:style>'
        )

    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:docDefaults><w:rPrDefault><w:rPr>'
        '<w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:eastAsia="Microsoft YaHei"/>'
        '<w:sz w:val="22"/><w:szCs w:val="22"/>'
        '</w:rPr></w:rPrDefault><w:pPrDefault><w:pPr>'
        '<w:spacing w:after="120" w:line="300" w:lineRule="auto"/>'
        '</w:pPr></w:pPrDefault></w:docDefaults>'
        + style("Normal", "Normal", 22, "000000", 0, 120)
        + style("ReportTitle", "Report Title", 48, "0B2545", 0, 120)
        + style("Subtitle", "Subtitle", 22, "555555", 0, 160)
        + style("Heading1", "Heading 1", 32, "2E74B5", 360, 200, True)
        + style("Heading2", "Heading 2", 26, "2E74B5", 280, 140, True)
        + style("Heading3", "Heading 3", 24, "1F4D78", 200, 100, True)
        + style("TableHeader", "Table Header", 18, "000000", 0, 0, True)
        + style("TableText", "Table Text", 18, "000000", 0, 0)
        + style("Muted", "Muted", 18, "555555", 0, 80)
        + "</w:styles>"
    )


def content_types_xml() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
  <Override PartName="/word/settings.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.settings+xml"/>
  <Override PartName="/word/fontTable.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.fontTable+xml"/>
  <Override PartName="/word/numbering.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml"/>
  <Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
  <Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
</Types>"""


def package_rels_xml() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>"""


def document_rels_xml() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/settings" Target="settings.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/fontTable" Target="fontTable.xml"/>
  <Relationship Id="rId4" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/numbering" Target="numbering.xml"/>
</Relationships>"""


def settings_xml() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:settings xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:zoom w:percent="100"/>
  <w:defaultTabStop w:val="720"/>
  <w:characterSpacingControl w:val="doNotCompress"/>
</w:settings>"""


def font_table_xml() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:fonts xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:font w:name="Calibri"/>
  <w:font w:name="Microsoft YaHei"/>
</w:fonts>"""


def numbering_xml() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:numbering xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"/>"""


def core_xml() -> str:
    now = dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
 xmlns:dc="http://purl.org/dc/elements/1.1/"
 xmlns:dcterms="http://purl.org/dc/terms/"
 xmlns:dcmitype="http://purl.org/dc/dcmitype/"
 xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <dc:title>Client 资源库统计报告</dc:title>
  <dc:creator>Codex</dc:creator>
  <cp:lastModifiedBy>Codex</cp:lastModifiedBy>
  <dcterms:created xsi:type="dcterms:W3CDTF">{now}</dcterms:created>
  <dcterms:modified xsi:type="dcterms:W3CDTF">{now}</dcterms:modified>
</cp:coreProperties>"""


def app_xml() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"
 xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">
  <Application>Codex</Application>
</Properties>"""


def add_content(doc: Docx, data: dict) -> None:
    by_type = {row["resource_type"]: row for row in data["resource_type_counts"]}
    stats_by_type = {row["resource_type"]: row for row in data["file_count_stats"]}
    parent_by_type = {row["resource_type"]: row for row in data["parent_stats"]}
    desc_by_type = {row["resource_type"]: row for row in data["description_counts"]}

    formats_by_type: dict[str, list[dict]] = defaultdict(list)
    for row in data["file_format_counts"]:
        formats_by_type[row["resource_type"]].append(row)

    primary_formats_by_type: dict[str, list[dict]] = defaultdict(list)
    for row in data["primary_file_format_counts"]:
        primary_formats_by_type[row["resource_type"]].append(row)

    roles_by_type: dict[str, list[dict]] = defaultdict(list)
    for row in data["file_role_counts"]:
        roles_by_type[row["resource_type"]].append(row)

    preview_by_type: dict[str, list[dict]] = defaultdict(list)
    for row in data["preview_counts"]:
        preview_by_type[row["resource_type"]].append(row)

    state_by_type: dict[str, dict[str, int]] = defaultdict(dict)
    for row in data["state_by_type"]:
        state_by_type[row["resource_type"]][row["process_state"]] = row["resource_count"]

    total_files = next((r["row_count"] for r in data["table_counts"] if r["table_name"] == "resource_file"), 0)
    total_previews = next((r["row_count"] for r in data["table_counts"] if r["table_name"] == "resource_preview"), 0)
    total_desc_rows = data["overall_description"]["description_rows"]
    total_desc_resources = data["overall_description"]["resources_with_description"]

    doc.p("Client 资源库统计报告", style="ReportTitle")
    doc.p(
        f"生成日期：2026-05-28。统计口径：只读查询本地 Client SQLite 缓存库 {data['db_path']}；"
        "resource_count 按 resource_task 行数统计，格式分布按 resource_file.file_format 统计。",
        style="Subtitle",
    )

    candidate_note = []
    for item in data["client_db_candidates"]:
        if item["exists"]:
            candidate_note.append(f"{item['path']}：{fmt_int(item['size'])} 字节")
        else:
            candidate_note.append(f"{item['path']}：不存在")
    doc.p(
        "说明：检查到 Client 目录下的候选库为 " + "；".join(candidate_note)
        + "。当前有数据的是 data/databases/pipeline.db，因此本文以该库作为 Client 本地缓存库统计对象。",
        style="Muted",
    )

    doc.heading("一、总体结论", 1)
    doc.table(
        ["指标", "数量", "说明"],
        [
            ["资源任务 resource_task", fmt_int(data["total_resources"]), "资源级记录总数"],
            ["文件记录 resource_file", fmt_int(total_files), "一个资源可对应多个实体文件"],
            ["预览记录 resource_preview", fmt_int(total_previews), "含 primary/gallery 等预览角色"],
            ["描述记录 resource_description", fmt_int(total_desc_rows), f"覆盖 {fmt_int(total_desc_resources)} 个资源；可能存在重复生成记录"],
            ["资源状态", "; ".join(f"{r['process_state']} {fmt_int(r['resource_count'])}" for r in data["state_counts"]), "当前仅见 committed 与 description_failed"],
        ],
        [2600, 1900, 4860],
    )

    doc.heading("二、各类资源数量", 1)
    summary_rows = []
    for typ, row in by_type.items():
        stats = stats_by_type.get(typ, {})
        parents = parent_by_type.get(typ, {})
        states = state_by_type.get(typ, {})
        committed = states.get("committed", 0)
        failed = sum(v for k, v in states.items() if k != "committed")
        summary_rows.append(
            [
                type_label(typ),
                fmt_int(row["resource_count"]),
                fmt_pct(row["resource_count"], data["total_resources"]),
                f"均值 {stats.get('avg_files', 0)} / 最大 {fmt_int(stats.get('max_files', 0))}",
                f"根 {fmt_int(parents.get('root_resources', 0))} / 子 {fmt_int(parents.get('child_resources', 0))}",
                f"committed {fmt_int(committed)}; failed {fmt_int(failed)}",
                format_summary(primary_formats_by_type.get(typ, []), "primary_file_count", 4),
            ]
        )
    doc.table(
        ["资源类型", "资源数", "占比", "文件数", "父子关系", "状态", "主文件格式"],
        summary_rows,
        [1780, 1100, 900, 1320, 1200, 1420, 1640],
    )

    doc.heading("三、资源结构与格式说明", 1)
    structure_rows = []
    for typ in by_type:
        roles = "; ".join(
            f"{r['file_role']} {fmt_int(r['file_count'])}" for r in roles_by_type.get(typ, [])[:5]
        ) or "-"
        preview = "; ".join(
            f"{r['strategy']}/{r['role']}/{r['preview_format'] or '-'} {fmt_int(r['preview_count'])}"
            for r in preview_by_type.get(typ, [])[:4]
        ) or "-"
        desc = desc_by_type.get(typ, {})
        structure_rows.append(
            [
                type_label(typ),
                STRUCTURE_NOTES.get(typ, "资源级记录，文件与预览通过 task_id 关联。"),
                roles,
                format_summary(formats_by_type.get(typ, []), "file_count", 6),
                preview,
                f"{fmt_int(desc.get('resources_with_description', 0))} 个资源有描述",
            ]
        )
    doc.table(
        ["资源类型", "结构", "文件角色", "文件格式", "预览结构", "描述覆盖"],
        structure_rows,
        [1420, 2760, 1250, 1700, 1450, 780],
    )

    doc.heading("四、单图格式分布与样例", 1, page_break_before=True)
    doc.p(
        "单图是当前库中的主体类型。下面按主文件格式统计，并为每种格式抽取前三条样例。"
    )
    single_dist = []
    for row in primary_formats_by_type.get(SINGLE_IMAGE_RESOURCE_TYPE, []):
        single_dist.append(
            [
                row["file_format"],
                fmt_int(row["primary_file_count"]),
                fmt_int(row["resource_count"]),
                fmt_pct(row["resource_count"], by_type[SINGLE_IMAGE_RESOURCE_TYPE]["resource_count"]),
            ]
        )
    doc.table(["格式", "主文件数", "涉及资源数", "在单图中占比"], single_dist, [1600, 2000, 2200, 3560])

    sample_rows = []
    for fmt, samples in data["single_image_examples_by_format"].items():
        for sample in samples:
            sample_rows.append(
                [
                    fmt,
                    sample["id"],
                    sample["source"],
                    compact_text(sample["title"] or sample["resource_path"], 70),
                    compact_text(sample["file_name"], 60),
                    fmt_int(sample["file_size"]),
                    sample["process_state"],
                ]
            )
    doc.table(
        ["格式", "ID", "来源", "标题/路径", "文件名", "大小(B)", "状态"],
        sample_rows,
        [700, 760, 920, 3000, 2000, 1000, 980],
    )

    doc.heading("五、每类资源代表样例", 1, page_break_before=True)
    for typ, samples in data["examples_by_type"].items():
        doc.heading(type_label(typ), 2)
        rows_ = []
        for sample in samples:
            rows_.append(
                [
                    sample["id"],
                    sample["source"],
                    compact_text(sample["title"] or sample["resource_path"], 80),
                    compact_text(sample["primary_file"] or sample["resource_path"], 70),
                    f"{fmt_int(sample['file_count'])} 文件; {sample['formats'] or '-'}",
                    compact_text(sample["preview_strategies"] or "-", 40),
                    compact_text(sample["description_main"] or "", 120),
                ]
            )
        doc.table(
            ["ID", "来源", "标题/路径", "主文件", "文件结构", "预览", "描述摘录"],
            rows_,
            [680, 850, 1900, 1600, 1250, 780, 2300],
        )

    doc.heading("六、格式明细附录", 1, page_break_before=True)
    appendix_rows = []
    primary_lookup = {
        (row["resource_type"], row["file_format"]): row["primary_file_count"]
        for row in data["primary_file_format_counts"]
    }
    for row in data["file_format_counts"]:
        appendix_rows.append(
            [
                type_label(row["resource_type"]),
                row["file_format"],
                fmt_int(row["file_count"]),
                fmt_int(row["resource_count"]),
                fmt_int(primary_lookup.get((row["resource_type"], row["file_format"]), 0)),
            ]
        )
    doc.table(
        ["资源类型", "文件格式", "文件数", "涉及资源数", "主文件数"],
        appendix_rows,
        [3000, 1400, 1600, 1700, 1660],
    )

    doc.heading("七、表结构口径", 1)
    doc.p(
        "resource_task 是资源级主表，包含 resource_type、source_resource_id、title、pack_name、"
        "resource_path、parent_resource_id、process_state、resource_id 等字段。"
    )
    doc.p(
        "resource_file 通过 task_id 关联资源，记录 file_path、file_name、file_size、file_format、"
        "file_role、is_primary 等字段，是本文格式和文件结构统计的主要来源。"
    )
    doc.p(
        "resource_preview 记录预览策略、角色、路径、格式、尺寸和占位标记；resource_description "
        "记录描述文本，可能因为重跑而同一资源存在多条描述记录。"
    )


def write_docx(doc: Docx, out_path: Path) -> None:
    with ZipFile(out_path, "w", ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types_xml())
        zf.writestr("_rels/.rels", package_rels_xml())
        zf.writestr("word/document.xml", doc.document_xml())
        zf.writestr("word/_rels/document.xml.rels", document_rels_xml())
        zf.writestr("word/styles.xml", styles_xml())
        zf.writestr("word/settings.xml", settings_xml())
        zf.writestr("word/fontTable.xml", font_table_xml())
        zf.writestr("word/numbering.xml", numbering_xml())
        zf.writestr("docProps/core.xml", core_xml())
        zf.writestr("docProps/app.xml", app_xml())


def main() -> int:
    if not DB_PATH.exists():
        raise SystemExit(f"Database not found: {DB_PATH}")
    data = collect_data()
    doc = Docx()
    add_content(doc, data)
    write_docx(doc, OUT_PATH)
    print(f"Wrote {OUT_PATH.resolve()}")
    print(f"Resources: {fmt_int(data['total_resources'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
