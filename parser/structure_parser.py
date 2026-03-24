"""
parser/structure_parser.py
===========================
Parses Datalab Marker API JSON output into a structured document tree.

Supports TWO document structures:

1. Part-only PDFs (e.g. BCBC Part 4, 145 pages):
   h1 -> Part
   h2 -> Section (2-part: 4.1)
   h3 -> Subsection (3-part: 4.1.6)
   h4 -> Article/Clause (4-part: 4.1.6.5)
   h5 -> Notes headings / Appendix entries
   h6 -> Sub-article or importance category label

2. Full building code PDFs (e.g. NBC 2020, 1530 pages):
   h1 -> Division A/B/C
   h2 -> Part 1/2/3...
   h3 -> Section (2-part: 3.1) or plain title
   h4 -> Article (3-part: 3.1.17)
   h5 -> Sentence/Clause (4-part: 3.1.17.1)
   h6 -> Sub-article or importance category label

The parser auto-detects which mode to use based on whether Division A/B/C
headings appear at h1 level.

FIX: The original parser was built for Part 4 only. When processing the
full NBC 2020 PDF, Division A/B/C appeared at h1 but had no handler,
causing them to become unnamed auto-numbered chapters. Parts at h2 were
orphaned entirely (RE_SECTION does not match "Part N"). This cascaded into
3-part section numbers like 3.1.17 having no parent Part, resulting in
"No sections in this chapter" and "No clauses in this section" in the viewer.
"""

import re
import os
import base64
from dataclasses import dataclass, field, asdict
from typing import List, Optional, Any
from datetime import datetime


# =============================================================================
# Data models
# =============================================================================

@dataclass
class ContentItem:
    """
    One item in the ordered content sequence of a Clause.
    type is one of: "text" | "equation" | "figure" | "table" | "sub_clause"
    All other fields are optional depending on type.
    """
    type: str

    # text
    value: str = ""

    # equation
    latex: str = ""

    # figure
    figure_id: str = ""
    image_key: str = ""
    image_path: str = ""     # relative path: storage/figures/{image_key}
    caption: str = ""
    alt_text: str = ""

    # table (inline reference — full table data also stored in tables[])
    table_id: str = ""

    # sub_clause marker
    marker: str = ""


@dataclass
class Table:
    id: str
    caption: str
    headers: List[str]
    rows: List[List[str]]
    page: int = 0


@dataclass
class Figure:
    id: str
    caption: str
    alt_text: str
    image_key: str
    image_path: str          # relative path for viewer
    page: int = 0


@dataclass
class Equation:
    id: str
    latex: str
    page: int = 0


@dataclass
class Clause:
    id: str
    number: str
    title: str
    # Ordered mixed content — preserves reading sequence
    content: List[ContentItem] = field(default_factory=list)
    # Typed indexes for backwards compatibility and quick access
    tables: List[Table] = field(default_factory=list)
    figures: List[Figure] = field(default_factory=list)
    equations: List[Equation] = field(default_factory=list)
    references: List[dict] = field(default_factory=list)
    page_span: List[int] = field(default_factory=list)


@dataclass
class Section:
    id: str
    number: str
    title: str
    clauses: List[Clause] = field(default_factory=list)
    page_span: List[int] = field(default_factory=list)


@dataclass
class Chapter:
    id: str
    number: str
    title: str
    sections: List[Section] = field(default_factory=list)
    page_span: List[int] = field(default_factory=list)


@dataclass
class Document:
    title: str
    source_pdf: str
    total_pages: int
    extracted_at: str
    chapters: List[Chapter] = field(default_factory=list)


# =============================================================================
# Regex patterns
# =============================================================================

# h1 (Part-only PDFs): "Part 4Structural Design" or "Part 4 Structural Design"
RE_PART = re.compile(r'^Part\s*(\d+)\s*(.*)', re.IGNORECASE)

# NEW: h1 (Full PDFs): "Division A" / "Division B" / "Division C"
RE_DIVISION = re.compile(r'^Division\s+([A-Z])\s*(.*)', re.IGNORECASE)

# h2/h3: "Section 4.1." or "4.1. Title"
RE_SECTION = re.compile(r'^(?:Section\s+)?(\d+\.\d+)\.?\s*(.*)', re.IGNORECASE)

# h3: "1.1.1. Title" - 3-part
RE_ARTICLE = re.compile(r'^(\d+\.\d+\.\d+)\.?\s*(.*)')

# h4: "1.1.1.1. Title" - 4-part  ALWAYS check before RE_ARTICLE
RE_SENTENCE = re.compile(r'^(\d+\.\d+\.\d+\.\d+)\.?\s*(.*)')

# sub-clause markers
RE_SUBCLAUSE = re.compile(r'^\s*(\([a-z]+\)|[a-z]\)|[ivxlcdm]+\.)\s+(.+)', re.IGNORECASE)

# cross-references
RE_REFERENCE = re.compile(
    r'(?:Sentence|Article|Subsection|Section|Table|Figure)\s+'
    r'([\d\.]+[\w\.\-\(\)]*)',
    re.IGNORECASE
)

# Figure caption number extraction e.g. "Figure 4.1.6.5.-A"
RE_FIGURE_NUM = re.compile(r'Figure\s+([\d\.]+[\w\.\-]*)', re.IGNORECASE)


# =============================================================================
# HTML helpers
# =============================================================================

def strip_html(html: str) -> str:
    """Remove HTML tags, decode entities, normalise whitespace."""
    if not html:
        return ""
    text = re.sub(r'<\s*/?\s*[A-Za-z][^>]*>', ' ', html)
    text = (text
            .replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')
            .replace('&nbsp;', ' ').replace('&#39;', "'").replace('&quot;', '"'))
    return re.sub(r'\s+', ' ', text).strip()


_INLINE_MATH_SPLIT = re.compile(
    r'(<math[^>]*>.*?</math>)',
    re.DOTALL | re.IGNORECASE
)


def _strip_html_keep_text(html: str) -> str:
    text = re.sub(r'<\s*/?\s*(?!math)[A-Za-z][^>]*>', ' ', html)
    text = (text
            .replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')
            .replace('&nbsp;', ' ').replace('&#39;', "'").replace('&quot;', '"'))
    return re.sub(r'\s+', ' ', text).strip()


def inline_math_to_markdown(html: str) -> str:
    """Convert inline <math> tags to $...$ markdown notation."""
    def _clean_latex(inner: str) -> str:
        latex = inner.strip()
        latex = latex.replace('\\\\', '\\')
        latex = (latex.replace('&amp;', '&').replace('&lt;', '<')
                 .replace('&gt;', '>').replace('&nbsp;', ' '))
        return re.sub(r'\s+', ' ', latex).strip()

    result = re.sub(
        r'<math[^>]*>(.*?)</math>',
        lambda m: f'${_clean_latex(m.group(1))}$',
        html,
        flags=re.DOTALL | re.IGNORECASE
    )
    result = re.sub(r'<\s*/?\s*[A-Za-z][^>]*>', ' ', result)
    result = (result
              .replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')
              .replace('&nbsp;', ' ').replace('&#39;', "'").replace('&quot;', '"'))
    return re.sub(r'\s+', ' ', result).strip()


def split_inline_math(html: str) -> list:
    """Legacy compatibility shim."""
    value = inline_math_to_markdown(html)
    if value:
        return [{"type": "text", "value": value}]
    return []


def extract_math(html: str) -> list:
    """Extract LaTeX from <math> tags in an Equation block."""
    raw_parts = re.findall(r'<math[^>]*>(.*?)</math>', html,
                           re.DOTALL | re.IGNORECASE)
    result = []
    for p in raw_parts:
        latex = p.strip()
        latex = latex.replace('\\\\', '\\')
        latex = (latex.replace('&amp;', '&').replace('&lt;', '<')
                 .replace('&gt;', '>').replace('&nbsp;', ' '))
        latex = re.sub(r'\s+', ' ', latex).strip()
        if latex:
            result.append(latex)
    return result


def parse_heading(html: str):
    """Extract (level, plain_text) from a SectionHeader HTML block."""
    m = re.match(r'<h(\d)[^>]*>(.*?)</h\1>', html.strip(), re.DOTALL | re.IGNORECASE)
    if m:
        return int(m.group(1)), strip_html(m.group(2))
    return 0, strip_html(html)


def listgroup_to_lines(html: str) -> str:
    """Convert ListGroup HTML to newline-separated lines, preserving inline math."""
    text_with_newlines = re.sub(r'</li>', '\n', html, flags=re.IGNORECASE)
    lines = []
    for raw_line in text_with_newlines.splitlines():
        if re.search(r'<math', raw_line, re.IGNORECASE):
            line = inline_math_to_markdown(raw_line)
        else:
            line = re.sub(r'<[^>]+>', '', raw_line)
            line = (line.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')
                    .replace('&nbsp;', ' ').replace('&#39;', "'").replace('&quot;', '"'))
        line = re.sub(r'[ \t]+', ' ', line).strip()
        if line:
            lines.append(line)
    return '\n'.join(lines)


def parse_table_html(html: str):
    """
    Parse HTML table into (headers, rows) with correct rowspan and colspan handling.
    """
    headers: list = []

    thead = re.search(r'<thead[^>]*>(.*?)</thead>', html, re.DOTALL | re.IGNORECASE)
    if thead:
        thead_html   = thead.group(1)
        header_rows  = re.findall(r'<tr[^>]*>(.*?)</tr>', thead_html,
                                  re.DOTALL | re.IGNORECASE)

        if len(header_rows) <= 1:
            ths     = re.findall(r'<th[^>]*>(.*?)</th>', thead_html,
                                 re.DOTALL | re.IGNORECASE)
            headers = [
                inline_math_to_markdown(th) if re.search(r'<math', th, re.IGNORECASE)
                else strip_html(th)
                for th in ths
            ]
        else:
            first_ths = re.findall(r'<th([^>]*)>(.*?)</th>', header_rows[0],
                                   re.DOTALL | re.IGNORECASE)
            num_cols = 0
            for attrs, _ in first_ths:
                cs = re.search(r'colspan=["\'](\d+)["\']', attrs)
                num_cols += int(cs.group(1)) if cs else 1

            n_rows     = len(header_rows)
            label_grid = [[''] * num_cols for _ in range(n_rows)]
            th_carry   = {}
            row_new_cells: list = [[] for _ in range(n_rows)]

            for row_i, tr_html in enumerate(header_rows):
                th_matches = re.findall(r'<th([^>]*)>(.*?)</th>', tr_html,
                                        re.DOTALL | re.IGNORECASE)
                th_iter = iter(th_matches)
                col     = 0

                while col < num_cols:
                    if col in th_carry:
                        remaining, label = th_carry[col]
                        label_grid[row_i][col] = label
                        if remaining - 1 > 0:
                            th_carry[col] = (remaining - 1, label)
                        else:
                            del th_carry[col]
                        col += 1
                        continue

                    try:
                        attrs, cell_html = next(th_iter)
                    except StopIteration:
                        col += 1
                        continue

                    if re.search(r'<math', cell_html, re.IGNORECASE):
                        label = inline_math_to_markdown(cell_html)
                    else:
                        label = strip_html(cell_html)

                    rs      = re.search(r'rowspan=["\'](\d+)["\']', attrs)
                    cs      = re.search(r'colspan=["\'](\d+)["\']', attrs)
                    rowspan = int(rs.group(1)) if rs else 1
                    colspan = int(cs.group(1)) if cs else 1

                    for c in range(colspan):
                        if col + c < num_cols:
                            label_grid[row_i][col + c] = label

                    if rowspan > 1:
                        for c in range(colspan):
                            if col + c < num_cols:
                                th_carry[col + c] = (rowspan - 1, label)

                    row_new_cells[row_i].append((col, colspan, label))
                    col += colspan

            spanning_rows: set = set()
            for row_i in range(n_rows):
                if row_i == n_rows - 1:
                    continue
                new = row_new_cells[row_i]
                if len(new) == 1 and new[0][1] > 1:
                    spanning_rows.add(row_i)

            headers = []
            seen_names: dict = {}
            for col in range(num_cols):
                parts = []
                for row_i in range(n_rows):
                    lbl = label_grid[row_i][col].strip()
                    if not lbl:
                        continue
                    if parts and parts[-1] == lbl:
                        continue
                    if (col == 0
                            and row_i == n_rows - 1 and parts
                            and len(lbl) <= 4
                            and re.match(r'^[0-9A-Z]+$', lbl)
                            and len(parts[-1]) > len(lbl) + 2):
                        continue
                    parts.append(lbl)
                name = ' / '.join(parts) if parts else f"Col {col+1}"
                if name in seen_names:
                    seen_names[name] += 1
                    name = f"{name} ({seen_names[name]})"
                else:
                    seen_names[name] = 1
                headers.append(name)

    num_cols = len(headers) if headers else 2

    rows: list = []

    tbody = re.search(r'<tbody[^>]*>(.*?)</tbody>', html, re.DOTALL | re.IGNORECASE)
    if not tbody:
        return headers, rows

    trs = re.findall(r'<tr[^>]*>(.*?)</tr>', tbody.group(1), re.DOTALL | re.IGNORECASE)
    rowspan_carry = {}

    for tr in trs:
        td_matches = re.findall(r'<td([^>]*)>(.*?)</td>', tr, re.DOTALL | re.IGNORECASE)
        row     = [''] * num_cols
        td_iter = iter(td_matches)
        col     = 0

        while col < num_cols:
            if col in rowspan_carry:
                remaining, value = rowspan_carry[col]
                row[col] = value
                if remaining - 1 > 0:
                    rowspan_carry[col] = (remaining - 1, value)
                else:
                    del rowspan_carry[col]
                col += 1
                continue

            try:
                attrs_str, cell_html = next(td_iter)
            except StopIteration:
                col += 1
                continue

            cell_value = strip_html(cell_html)
            rs = re.search(r'rowspan=["\'](\d+)["\']', attrs_str)
            cs = re.search(r'colspan=["\'](\d+)["\']', attrs_str)
            rowspan = int(rs.group(1)) if rs else 1
            colspan = int(cs.group(1)) if cs else 1

            if colspan >= num_cols:
                row[0] = cell_value
            else:
                for c in range(colspan):
                    if col + c < num_cols:
                        row[col + c] = cell_value

            if rowspan > 1:
                for c in range(min(colspan, num_cols)):
                    if col + c < num_cols:
                        rowspan_carry[col + c] = (rowspan - 1, cell_value)

            col += colspan

        if any(c.strip() for c in row):
            rows.append(row)

    row_bboxes: list = []
    trs_all = re.findall(r'<tr[^>]*>(.*?)</tr>',
                         (tbody.group(1) if tbody else ""),
                         re.DOTALL | re.IGNORECASE)

    for tr in trs_all:
        td_matches = re.findall(r'<td([^>]*)>', tr, re.IGNORECASE)
        if len(td_matches) < 2:
            continue
        use_attrs  = td_matches[0]
        load_attrs = td_matches[1]

        def _y(attrs_str, coord_idx):
            m = re.search(r'data-bbox=["\']([^"\']+)["\']', attrs_str)
            if not m:
                return None
            parts = m.group(1).split()
            return int(parts[coord_idx]) if len(parts) == 4 else None

        use_y1   = _y(use_attrs,  1)
        load_y2  = _y(load_attrs, 3)

        td_content = re.findall(r'<td[^>]*>(.*?)</td>', tr,
                                re.DOTALL | re.IGNORECASE)
        row_texts = [re.sub(r'<[^>]+>', '', c).strip() for c in td_content]
        if any(t for t in row_texts):
            row_bboxes.append((use_y1, load_y2))

    if row_bboxes and len(row_bboxes) == len(rows):
        col_last_val  = {}
        col_last_y2   = {}

        for row_i, (row, (use_y1, load_y2)) in enumerate(zip(rows, row_bboxes)):
            for col_idx in range(len(row)):
                val = row[col_idx]
                if val.strip():
                    col_last_val[col_idx] = val
                    if load_y2 is not None:
                        col_last_y2[col_idx] = load_y2
                else:
                    if (col_idx in col_last_val
                            and col_idx in col_last_y2
                            and use_y1 is not None
                            and col_last_y2[col_idx] > use_y1):
                        rows[row_i][col_idx] = col_last_val[col_idx]

    return headers, rows


def extract_alt_text(html: str) -> str:
    """Extract alt attribute from <img> tag."""
    m = re.search(r'<img[^>]+alt=["\']([^"\']*)["\']', html, re.IGNORECASE)
    return m.group(1).strip() if m else strip_html(html)


def save_image(image_key: str, base64_data: str, figures_dir: str) -> str:
    """Decode base64 image and save to disk."""
    os.makedirs(figures_dir, exist_ok=True)
    file_path = os.path.join(figures_dir, image_key)
    if not os.path.exists(file_path):
        try:
            img_bytes = base64.b64decode(base64_data)
            with open(file_path, 'wb') as f:
                f.write(img_bytes)
        except Exception as e:
            print(f"[Parser] Warning: could not save image {image_key}: {e}")
            return ""
    return os.path.join("storage", "figures", image_key)


# =============================================================================
# Document mode detection
# =============================================================================

def _detect_document_mode(blocks: list) -> str:
    """
    Detect whether this is a full multi-Division PDF or a Part-only PDF.

    Returns:
        'full'  — Full PDF with Division A/B/C at h1 level
                  (e.g. NBC 2020 complete, 1530 pages)
        'part'  — Part-only PDF with Part N at h1 level
                  (e.g. BCBC Part 4, 145 pages)

    Strategy: scan first 100 heading blocks. If any h1 matches Division A/B/C,
    it is a full document. If any h1 matches Part N, it is a part-only document.
    Default to 'part' if nothing decisive is found.
    """
    checked = 0
    for block in blocks:
        if block.get("type") != "heading":
            continue
        level = block.get("level", 0)
        text  = block.get("text", "")
        if level == 1:
            if RE_DIVISION.match(text):
                return 'full'
            if RE_PART.match(text):
                return 'part'
        checked += 1
        if checked >= 100:
            break
    return 'part'


# =============================================================================
# Main parser
# =============================================================================

class StructureParser:

    def __init__(self, source_pdf: str = "unknown.pdf",
                 figures_dir: str = "storage/figures"):
        self.source_pdf  = source_pdf
        self.figures_dir = figures_dir
        self._chapter_counter    = 0
        self._auto_clause_counter = 0
        self._table_counter      = 0
        self._equation_counter   = 0
        self._figure_counter     = 0
        self._images_dict        = {}
        self._doc_mode           = 'part'  # set after block flattening

    def parse(self, datalab_result: dict) -> Document:
        self._images_dict = datalab_result.get("images") or {}
        blocks = self._flatten_blocks(datalab_result)
        total_pages = (
            datalab_result.get("page_count") or
            len((datalab_result.get("json") or {}).get("children", []))
        )

        # Auto-detect document structure BEFORE building hierarchy
        self._doc_mode = _detect_document_mode(blocks)
        print(f"[Parser] Document mode detected: '{self._doc_mode}' "
              f"({'Full multi-Division PDF' if self._doc_mode == 'full' else 'Part-only PDF'})")

        document = Document(
            title=self._detect_title(blocks),
            source_pdf=self.source_pdf,
            total_pages=total_pages or 0,
            extracted_at=datetime.utcnow().isoformat() + "Z",
        )
        document.chapters = self._build_hierarchy(blocks)
        return document

    # -------------------------------------------------------------------------
    # Block flattening
    # -------------------------------------------------------------------------

    def _flatten_blocks(self, datalab_result: dict) -> list:
        flat = []
        json_output  = datalab_result.get("json") or {}
        page_objects = json_output.get("children", [])

        if not page_objects:
            return self._flatten_legacy(datalab_result)

        for page_obj in page_objects:
            if page_obj.get("block_type") != "Page":
                continue
            try:
                page_num = int(page_obj["id"].split("/page/")[1].split("/")[0]) + 1
            except (IndexError, ValueError, KeyError):
                page_num = 1

            children = page_obj.get("children", [])

            for idx, block in enumerate(children):
                btype_raw = block.get("block_type", "")
                html      = (block.get("html") or "").strip()

                if btype_raw in ("PageFooter", "PageHeader"):
                    continue
                if not html:
                    continue

                if btype_raw == "SectionHeader":
                    level, text = parse_heading(html)
                    flat.append({"type": "heading", "level": level,
                                 "text": text, "page": page_num, "raw": block})

                elif btype_raw == "ListGroup":
                    text = listgroup_to_lines(html)
                    if text:
                        flat.append({"type": "text", "level": 0,
                                     "text": text, "page": page_num, "raw": block})

                elif btype_raw == "Equation":
                    latex_list = extract_math(html)
                    if not latex_list:
                        text = strip_html(html)
                        if text:
                            latex_list = [text]
                    for latex in latex_list:
                        flat.append({"type": "equation", "level": 0,
                                     "text": latex, "latex": latex,
                                     "page": page_num, "raw": block})

                elif btype_raw in ("Figure", "Picture"):
                    block_images = block.get("images") or {}
                    image_key    = next(iter(block_images.keys()), "")
                    alt_text     = extract_alt_text(html)

                    alt_lower = alt_text.lower().strip()
                    if alt_lower in ("horizontal line", "vertical line",
                                     "divider", "line", "rule", "separator"):
                        continue

                    caption = self._find_figure_caption(children, idx, alt_text)

                    flat.append({"type": "figure", "level": 0,
                                 "text": alt_text, "image_key": image_key,
                                 "alt_text": alt_text, "caption": caption,
                                 "page": page_num, "raw": block})

                elif btype_raw == "Caption":
                    if re.search(r'<math', html, re.IGNORECASE):
                        text = inline_math_to_markdown(html)
                    else:
                        text = strip_html(html)
                    if text:
                        flat.append({"type": "caption", "level": 0,
                                     "text": text, "page": page_num, "raw": block})

                elif btype_raw == "Table":
                    flat.append({"type": "table", "level": 0,
                                 "text": html, "page": page_num, "raw": block})

                else:
                    if re.search(r'<math', html, re.IGNORECASE):
                        flat.append({"type": "text", "level": 0,
                                     "text": html, "has_inline_math": True,
                                     "page": page_num, "raw": block})
                    else:
                        text = strip_html(html)
                        if text:
                            flat.append({"type": "text", "level": 0,
                                         "text": text, "page": page_num,
                                         "raw": block})

        return flat

    def _find_figure_caption(self, siblings: list, fig_idx: int,
                              alt_text: str) -> str:
        if fig_idx > 0:
            prev = siblings[fig_idx - 1]
            if prev.get("block_type") == "Caption":
                return strip_html(prev.get("html", ""))

        if fig_idx < len(siblings) - 1:
            nxt = siblings[fig_idx + 1]
            if nxt.get("block_type") == "Caption":
                return strip_html(nxt.get("html", ""))
            if nxt.get("block_type") == "SectionHeader":
                m = re.search(r'Notes to (Figure\s+[\w\.\-]+)',
                              nxt.get("html", ""), re.IGNORECASE)
                if m:
                    return m.group(1)

        m = RE_FIGURE_NUM.search(alt_text)
        if m:
            return f"Figure {m.group(1)}"

        return ""

    def _flatten_legacy(self, datalab_result: dict) -> list:
        """Fallback for old API format or markdown-only responses."""
        flat = []
        for page_num, page in enumerate(
                datalab_result.get("pages", []), start=1):
            for block in page.get("blocks", []):
                flat.append({
                    "type":  block.get("block_type", "text"),
                    "text":  block.get("html", block.get("text", "")).strip(),
                    "level": block.get("level", 0),
                    "page":  page_num, "raw": block,
                })
        if not flat and datalab_result.get("markdown"):
            for line in datalab_result["markdown"].splitlines():
                s = line.strip()
                if not s:
                    continue
                for prefix, lvl in [("#### ",4),("### ",3),("## ",2),("# ",1)]:
                    if s.startswith(prefix):
                        flat.append({"type":"heading","level":lvl,
                                     "text":s[len(prefix):],"page":1,"raw":{}})
                        break
                else:
                    flat.append({"type":"text","level":0,"text":s,"page":1,"raw":{}})
        return flat

    # -------------------------------------------------------------------------
    # Title detection
    # -------------------------------------------------------------------------

    def _detect_title(self, blocks: list) -> str:
        for b in blocks:
            if b["type"] == "heading" and b.get("level", 0) == 1:
                return b["text"]
        return "Building Code Document"

    # -------------------------------------------------------------------------
    # Hierarchy builder — dispatches to the correct mode
    # -------------------------------------------------------------------------

    def _build_hierarchy(self, blocks: list) -> List[Chapter]:
        if self._doc_mode == 'full':
            return self._build_hierarchy_full(blocks)
        else:
            return self._build_hierarchy_part(blocks)

    # -------------------------------------------------------------------------
    # Mode A: Part-only PDF (original logic, unchanged)
    # -------------------------------------------------------------------------

    def _build_hierarchy_part(self, blocks: list) -> List[Chapter]:
        """
        Original hierarchy builder for Part-only PDFs.
        h1=Part, h2=Section(2-part), h3=Subsection(3-part), h4=Clause(4-part)
        """
        chapters: List[Chapter] = []
        current_chapter: Optional[Chapter] = None
        current_section: Optional[Section] = None
        current_clause:  Optional[Clause]  = None
        pending_caption: str = ""

        def add_text(text: str, page: int, has_inline_math: bool = False):
            if not text or not current_clause:
                return
            if has_inline_math:
                markdown_text = inline_math_to_markdown(text)
                if markdown_text:
                    for line in markdown_text.splitlines():
                        sc = RE_SUBCLAUSE.match(line)
                        if sc:
                            current_clause.content.append(ContentItem(
                                type="sub_clause",
                                marker=sc.group(1),
                                value=sc.group(2).strip(),
                            ))
                        elif line.strip():
                            current_clause.content.append(ContentItem(
                                type="text", value=line.strip()
                            ))
                if page not in current_clause.page_span:
                    current_clause.page_span.append(page)
                return
            for line in text.splitlines():
                m = RE_SUBCLAUSE.match(line)
                if m:
                    current_clause.content.append(ContentItem(
                        type="sub_clause",
                        marker=m.group(1),
                        value=m.group(2).strip(),
                    ))
                else:
                    if line.strip():
                        current_clause.content.append(ContentItem(
                            type="text", value=line.strip()
                        ))
            if page not in current_clause.page_span:
                current_clause.page_span.append(page)

        for block in blocks:
            btype = block["type"]
            text  = block.get("text", "")
            page  = block["page"]
            level = block.get("level", 0)

            if btype == "heading":
                if level <= 1:
                    num, title = self._parse_part_heading(text)
                    current_chapter = Chapter(
                        id=f"CH-{num}", number=num,
                        title=title, page_span=[page]
                    )
                    chapters.append(current_chapter)
                    current_section = None
                    current_clause  = None

                elif level == 2:
                    m = RE_SECTION.match(text)
                    if m and current_chapter:
                        num, title = m.group(1), (m.group(2).strip() or m.group(1))
                        current_section = Section(
                            id=f"SEC-{num.replace('.', '-')}",
                            number=num, title=title, page_span=[page]
                        )
                        current_chapter.sections.append(current_section)
                        current_clause = None

                elif level == 3:
                    m3 = RE_ARTICLE.match(text)
                    if m3 and current_chapter:
                        num   = m3.group(1)
                        title = m3.group(2).lstrip(". ").strip() or num
                        current_section = Section(
                            id=f"SEC-{num.replace('.', '-')}",
                            number=num, title=title, page_span=[page]
                        )
                        current_chapter.sections.append(current_section)
                        current_clause = None
                    elif current_section:
                        current_clause = self._make_clause("", text, page,
                                                           current_section)

                elif level == 4:
                    m4 = RE_SENTENCE.match(text)
                    m3 = RE_ARTICLE.match(text)
                    if m4 and current_section:
                        num   = m4.group(1)
                        title = m4.group(2).lstrip(". ").strip() or num
                        current_clause = self._make_clause(num, title, page,
                                                           current_section)
                    elif m3 and current_section:
                        num   = m3.group(1)
                        title = m3.group(2).lstrip(". ").strip() or num
                        current_clause = self._make_clause(num, title, page,
                                                           current_section)
                    elif current_section:
                        current_clause = self._make_clause("", text, page,
                                                           current_section)

                elif level == 5:
                    clean = re.sub(r'\s+', ' ', strip_html(text)).strip()
                    if current_section:
                        current_clause = self._make_clause("", clean, page,
                                                           current_section)

                elif level >= 6:
                    clean = re.sub(r'\s+', ' ', strip_html(text)).strip()
                    if current_clause is not None:
                        current_clause.content.append(
                            ContentItem(type="text", value=f"**{clean}**"))
                        if page not in current_clause.page_span:
                            current_clause.page_span.append(page)
                    elif current_section:
                        current_clause = self._make_clause("", clean, page,
                                                           current_section)

            elif btype == "text":
                has_inline_math = block.get("has_inline_math", False)
                first_line = text.splitlines()[0] if text else ""
                check_line = strip_html(first_line) if has_inline_math else first_line
                m4  = RE_SENTENCE.match(check_line)
                m3  = RE_ARTICLE.match(check_line)
                sec = RE_SECTION.match(check_line)

                if m4 and current_section:
                    num   = m4.group(1)
                    cid   = f"CL-{num.replace('.', '-')}"
                    existing = any(cl.id == cid for cl in current_section.clauses)
                    if not existing:
                        title = m4.group(2).lstrip(". ").strip() or num
                        current_clause = self._make_clause(num, title, page,
                                                           current_section)
                    else:
                        add_text(text, page, has_inline_math)
                elif m3 and current_chapter:
                    num   = m3.group(1)
                    sid   = f"SEC-{num.replace('.', '-')}"
                    existing = any(s.id == sid for s in current_chapter.sections)
                    if not existing:
                        title = m3.group(2).lstrip(". ").strip() or num
                        current_section = Section(
                            id=sid, number=num,
                            title=title, page_span=[page]
                        )
                        current_chapter.sections.append(current_section)
                        current_clause = None
                    else:
                        add_text(text, page, has_inline_math)
                elif sec and current_chapter:
                    sid = f"SEC-{sec.group(1).replace('.', '-')}"
                    existing = any(s.id == sid for s in current_chapter.sections)
                    if not existing:
                        current_section = Section(
                            id=sid,
                            number=sec.group(1),
                            title=sec.group(2).strip(), page_span=[page]
                        )
                        current_chapter.sections.append(current_section)
                        current_clause = None
                    else:
                        add_text(text, page, has_inline_math)
                else:
                    add_text(text, page, has_inline_math)

            elif btype == "equation":
                if current_clause:
                    self._equation_counter += 1
                    eq_id  = f"EQ-{self._equation_counter}"
                    latex  = block.get("latex", text)
                    eq_obj = Equation(id=eq_id, latex=latex, page=page)
                    current_clause.equations.append(eq_obj)
                    current_clause.content.append(ContentItem(
                        type="equation", latex=latex, value=eq_id
                    ))
                    if page not in current_clause.page_span:
                        current_clause.page_span.append(page)

            elif btype == "figure":
                chapters, current_chapter, current_section, current_clause = \
                    self._handle_figure(block, page, chapters,
                                        current_chapter, current_section, current_clause)

            elif btype == "caption":
                pending_caption = text

            elif btype == "table":
                if current_clause:
                    self._table_counter += 1
                    tbl_id  = f"TBL-{self._table_counter}"
                    caption = pending_caption or f"Table {self._table_counter}"
                    pending_caption = ""
                    headers, rows = parse_table_html(text)
                    tbl_obj = Table(
                        id=tbl_id, caption=caption,
                        headers=headers, rows=rows, page=page
                    )
                    current_clause.tables.append(tbl_obj)
                    current_clause.content.append(ContentItem(
                        type="table", table_id=tbl_id, value=caption
                    ))
                    if page not in current_clause.page_span:
                        current_clause.page_span.append(page)
                else:
                    pending_caption = ""

        return self._merge_continued_tables(self._remove_empty_clauses(chapters))

    # -------------------------------------------------------------------------
    # Mode B: Full multi-Division PDF (NBC 2020 structure)
    # -------------------------------------------------------------------------

    def _build_hierarchy_full(self, blocks: list) -> List[Chapter]:
        """
        Hierarchy builder for full multi-Division PDFs.

        Full NBC 2020 heading structure:
            h1 -> Division A/B/C          -> Chapter
            h2 -> Part 1/2/3...           -> Chapter (nested under Division)
            h3 -> Section 3.1 (2-part)    -> Section
            h4 -> Article 3.1.17 (3-part) -> Section (subsection)
            h5 -> Sentence 3.1.17.1       -> Clause
            h6 -> Sub-article / label     -> inline content or clause

        Parts become Chapters directly (the viewer shows Chapter as the
        top navigation level). Division labels are prepended to chapter
        titles so navigation reads: "Division B — Part 3: Fire Protection".
        """
        chapters: List[Chapter] = []
        current_division: str   = ""   # e.g. "Division B"
        current_chapter: Optional[Chapter] = None
        current_section: Optional[Section] = None
        current_clause:  Optional[Clause]  = None
        pending_caption: str = ""

        def add_text(text: str, page: int, has_inline_math: bool = False):
            if not text or not current_clause:
                return
            if has_inline_math:
                markdown_text = inline_math_to_markdown(text)
                if markdown_text:
                    for line in markdown_text.splitlines():
                        sc = RE_SUBCLAUSE.match(line)
                        if sc:
                            current_clause.content.append(ContentItem(
                                type="sub_clause",
                                marker=sc.group(1),
                                value=sc.group(2).strip(),
                            ))
                        elif line.strip():
                            current_clause.content.append(ContentItem(
                                type="text", value=line.strip()
                            ))
                if page not in current_clause.page_span:
                    current_clause.page_span.append(page)
                return
            for line in text.splitlines():
                m = RE_SUBCLAUSE.match(line)
                if m:
                    current_clause.content.append(ContentItem(
                        type="sub_clause",
                        marker=m.group(1),
                        value=m.group(2).strip(),
                    ))
                else:
                    if line.strip():
                        current_clause.content.append(ContentItem(
                            type="text", value=line.strip()
                        ))
            if page not in current_clause.page_span:
                current_clause.page_span.append(page)

        for block in blocks:
            btype = block["type"]
            text  = block.get("text", "")
            page  = block["page"]
            level = block.get("level", 0)

            if btype == "heading":

                # ── h1: Division A / Division B / Division C ─────────────────
                if level <= 1:
                    div_m  = RE_DIVISION.match(text)
                    part_m = RE_PART.match(text)

                    if div_m:
                        # "Division A" — store as context label, not a chapter.
                        # Parts underneath it will become chapters with this prefix.
                        current_division = f"Division {div_m.group(1).upper()}"
                        # Also create a chapter for the Division itself so its
                        # introductory content (before any Part heading) is not lost.
                        self._chapter_counter += 1
                        current_chapter = Chapter(
                            id=f"CH-DIV-{div_m.group(1).upper()}",
                            number=div_m.group(1).upper(),
                            title=current_division
                                  + (f" — {div_m.group(2).strip()}"
                                     if div_m.group(2).strip() else ""),
                            page_span=[page]
                        )
                        chapters.append(current_chapter)
                        current_section = None
                        current_clause  = None

                    elif part_m:
                        # Bare "Part N" at h1 (Part-only PDF slipped through detection)
                        # Treat as chapter — same as part mode
                        num, title = self._parse_part_heading(text)
                        current_chapter = Chapter(
                            id=f"CH-{num}", number=num,
                            title=title, page_span=[page]
                        )
                        chapters.append(current_chapter)
                        current_section = None
                        current_clause  = None

                    else:
                        # Unrecognised h1 — treat as unnamed chapter
                        self._chapter_counter += 1
                        current_chapter = Chapter(
                            id=f"CH-AUTO-{self._chapter_counter}",
                            number=str(self._chapter_counter),
                            title=text, page_span=[page]
                        )
                        chapters.append(current_chapter)
                        current_section = None
                        current_clause  = None

                # ── h2: Part 1, Part 2, Part 3 ... ───────────────────────────
                elif level == 2:
                    part_m = RE_PART.match(text)
                    sec_m  = RE_SECTION.match(text)

                    if part_m:
                        # "Part 3" under Division B -> new Chapter
                        part_num   = part_m.group(1)
                        part_title = part_m.group(2).strip()
                        div_prefix = f"{current_division} — " if current_division else ""
                        full_title = f"{div_prefix}Part {part_num}"
                        if part_title:
                            full_title += f" — {part_title}"
                        self._chapter_counter += 1
                        current_chapter = Chapter(
                            id=f"CH-{current_division.replace(' ', '')}-P{part_num}",
                            number=f"{current_division} Part {part_num}",
                            title=full_title,
                            page_span=[page]
                        )
                        chapters.append(current_chapter)
                        current_section = None
                        current_clause  = None

                    elif sec_m and current_chapter:
                        # "Section 3.1." or plain 2-part number at h2
                        num   = sec_m.group(1)
                        title = sec_m.group(2).strip() or num
                        current_section = Section(
                            id=f"SEC-{num.replace('.', '-')}",
                            number=num, title=title, page_span=[page]
                        )
                        current_chapter.sections.append(current_section)
                        current_clause = None

                    elif current_chapter:
                        # Plain title like "General", "Compliance" at h2
                        # Create a label section to hold following content
                        self._auto_clause_counter += 1
                        sid = f"SEC-AUTO-{self._auto_clause_counter}"
                        current_section = Section(
                            id=sid, number="", title=text, page_span=[page]
                        )
                        current_chapter.sections.append(current_section)
                        current_clause = None

                # ── h3: Section 3.1. (2-part) or plain subsection title ───────
                elif level == 3:
                    sec_m = RE_SECTION.match(text)
                    m3    = RE_ARTICLE.match(text)

                    if sec_m and current_chapter:
                        # "3.1." or "Section 3.1." — primary section
                        num   = sec_m.group(1)
                        title = sec_m.group(2).strip() or num
                        current_section = Section(
                            id=f"SEC-{num.replace('.', '-')}",
                            number=num, title=title, page_span=[page]
                        )
                        current_chapter.sections.append(current_section)
                        current_clause = None

                    elif m3 and current_chapter:
                        # 3-part number at h3 (e.g. Notes to Part appendix)
                        num   = m3.group(1)
                        title = m3.group(2).lstrip(". ").strip() or num
                        current_section = Section(
                            id=f"SEC-{num.replace('.', '-')}",
                            number=num, title=title, page_span=[page]
                        )
                        current_chapter.sections.append(current_section)
                        current_clause = None

                    elif current_section:
                        # Plain title — label clause within current section
                        current_clause = self._make_clause("", text, page,
                                                           current_section)
                    elif current_chapter:
                        # Create section to hold content
                        self._auto_clause_counter += 1
                        sid = f"SEC-AUTO-{self._auto_clause_counter}"
                        current_section = Section(
                            id=sid, number="", title=text, page_span=[page]
                        )
                        current_chapter.sections.append(current_section)
                        current_clause = None

                # ── h4: Article 3.1.17. (3-part number) ──────────────────────
                elif level == 4:
                    m4 = RE_SENTENCE.match(text)   # 4-part (check first)
                    m3 = RE_ARTICLE.match(text)    # 3-part

                    if m4 and current_section:
                        # 4-part at h4: treat as clause directly
                        num   = m4.group(1)
                        title = m4.group(2).lstrip(". ").strip() or num
                        current_clause = self._make_clause(num, title, page,
                                                           current_section)

                    elif m3 and current_chapter:
                        # 3-part article: becomes a sub-section
                        num   = m3.group(1)
                        title = m3.group(2).lstrip(". ").strip() or num
                        sid   = f"SEC-{num.replace('.', '-')}"

                        # Only create if not already registered
                        existing = None
                        if current_chapter:
                            existing = next(
                                (s for s in current_chapter.sections
                                 if s.id == sid), None
                            )
                        if not existing:
                            current_section = Section(
                                id=sid, number=num,
                                title=title, page_span=[page]
                            )
                            current_chapter.sections.append(current_section)
                            current_clause = None
                        else:
                            current_section = existing

                    elif current_section:
                        # Plain title at h4
                        current_clause = self._make_clause("", text, page,
                                                           current_section)

                # ── h5: Sentence 3.1.17.1. (4-part number) or notes ──────────
                elif level == 5:
                    m4 = RE_SENTENCE.match(text)
                    m3 = RE_ARTICLE.match(text)

                    if m4 and current_section:
                        num   = m4.group(1)
                        title = m4.group(2).lstrip(". ").strip() or num
                        current_clause = self._make_clause(num, title, page,
                                                           current_section)

                    elif m3 and current_section:
                        # 3-part at h5 in full doc — treat as clause
                        num   = m3.group(1)
                        title = m3.group(2).lstrip(". ").strip() or num
                        current_clause = self._make_clause(num, title, page,
                                                           current_section)

                    else:
                        # Notes headings, appendix entries
                        clean = re.sub(r'\s+', ' ', strip_html(text)).strip()
                        if current_section:
                            current_clause = self._make_clause("", clean, page,
                                                               current_section)

                # ── h6+: sub-labels ───────────────────────────────────────────
                elif level >= 6:
                    clean = re.sub(r'\s+', ' ', strip_html(text)).strip()
                    if current_clause is not None:
                        current_clause.content.append(
                            ContentItem(type="text", value=f"**{clean}**"))
                        if page not in current_clause.page_span:
                            current_clause.page_span.append(page)
                    elif current_section:
                        current_clause = self._make_clause("", clean, page,
                                                           current_section)

            # ── Text ──────────────────────────────────────────────────────────
            elif btype == "text":
                has_inline_math = block.get("has_inline_math", False)
                first_line = text.splitlines()[0] if text else ""
                check_line = strip_html(first_line) if has_inline_math else first_line

                m4  = RE_SENTENCE.match(check_line)
                m3  = RE_ARTICLE.match(check_line)
                sec = RE_SECTION.match(check_line)

                if m4 and current_section:
                    num   = m4.group(1)
                    cid   = f"CL-{num.replace('.', '-')}"
                    existing = any(cl.id == cid for cl in current_section.clauses)
                    if not existing:
                        title = m4.group(2).lstrip(". ").strip() or num
                        current_clause = self._make_clause(num, title, page,
                                                           current_section)
                    else:
                        add_text(text, page, has_inline_math)

                elif m3 and current_chapter:
                    num   = m3.group(1)
                    sid   = f"SEC-{num.replace('.', '-')}"
                    existing = any(s.id == sid for s in current_chapter.sections)
                    if not existing:
                        title = m3.group(2).lstrip(". ").strip() or num
                        current_section = Section(
                            id=sid, number=num,
                            title=title, page_span=[page]
                        )
                        current_chapter.sections.append(current_section)
                        current_clause = None
                    else:
                        add_text(text, page, has_inline_math)

                elif sec and current_chapter:
                    sid = f"SEC-{sec.group(1).replace('.', '-')}"
                    existing = any(s.id == sid for s in current_chapter.sections)
                    if not existing:
                        current_section = Section(
                            id=sid,
                            number=sec.group(1),
                            title=sec.group(2).strip(), page_span=[page]
                        )
                        current_chapter.sections.append(current_section)
                        current_clause = None
                    else:
                        add_text(text, page, has_inline_math)
                else:
                    add_text(text, page, has_inline_math)

            # ── Equation ──────────────────────────────────────────────────────
            elif btype == "equation":
                if current_clause:
                    self._equation_counter += 1
                    eq_id  = f"EQ-{self._equation_counter}"
                    latex  = block.get("latex", text)
                    eq_obj = Equation(id=eq_id, latex=latex, page=page)
                    current_clause.equations.append(eq_obj)
                    current_clause.content.append(ContentItem(
                        type="equation", latex=latex, value=eq_id
                    ))
                    if page not in current_clause.page_span:
                        current_clause.page_span.append(page)

            # ── Figure ────────────────────────────────────────────────────────
            elif btype == "figure":
                chapters, current_chapter, current_section, current_clause = \
                    self._handle_figure(block, page, chapters,
                                        current_chapter, current_section, current_clause)

            # ── Caption ───────────────────────────────────────────────────────
            elif btype == "caption":
                pending_caption = text

            # ── Table ─────────────────────────────────────────────────────────
            elif btype == "table":
                if current_clause:
                    self._table_counter += 1
                    tbl_id  = f"TBL-{self._table_counter}"
                    caption = pending_caption or f"Table {self._table_counter}"
                    pending_caption = ""
                    headers, rows = parse_table_html(text)
                    tbl_obj = Table(
                        id=tbl_id, caption=caption,
                        headers=headers, rows=rows, page=page
                    )
                    current_clause.tables.append(tbl_obj)
                    current_clause.content.append(ContentItem(
                        type="table", table_id=tbl_id, value=caption
                    ))
                    if page not in current_clause.page_span:
                        current_clause.page_span.append(page)
                else:
                    pending_caption = ""

        return self._merge_continued_tables(self._remove_empty_clauses(chapters))

    # -------------------------------------------------------------------------
    # Shared figure handler (used by both modes)
    # -------------------------------------------------------------------------

    def _handle_figure(self, block, page, chapters,
                       current_chapter, current_section, current_clause):
        """Handle a figure block — shared logic for both hierarchy modes."""
        self._figure_counter += 1
        fig_id    = f"FIG-{self._figure_counter}"
        image_key = block.get("image_key", "")
        alt_text  = block.get("alt_text", "")
        caption   = block.get("caption", "")

        image_path = ""
        if image_key and image_key in self._images_dict:
            image_path = save_image(
                image_key,
                self._images_dict[image_key],
                self.figures_dir
            )

        fig_obj = Figure(
            id=fig_id, caption=caption, alt_text=alt_text,
            image_key=image_key, image_path=image_path, page=page
        )
        content_item = ContentItem(
            type="figure", figure_id=fig_id,
            image_key=image_key, image_path=image_path,
            caption=caption, alt_text=alt_text
        )

        alt_stripped = alt_text.strip().lower()
        is_decorative = (
            len(alt_stripped) < 60 and
            any(kw == alt_stripped or alt_stripped.startswith(kw)
                for kw in ("horizontal line", "vertical line", "divider",
                           "separator", "solid black line", "decorative"))
        )

        if is_decorative:
            self._figure_counter -= 1
            return chapters, current_chapter, current_section, current_clause

        if current_clause:
            current_clause.figures.append(fig_obj)
            current_clause.content.append(content_item)
            if page not in current_clause.page_span:
                current_clause.page_span.append(page)
        elif current_section:
            orphan = self._make_clause(
                "", caption or alt_text[:60] or f"Figure {fig_id}",
                page, current_section
            )
            orphan.figures.append(fig_obj)
            orphan.content.append(content_item)
            current_clause = orphan

        return chapters, current_chapter, current_section, current_clause

    # -------------------------------------------------------------------------
    # Post-processing (shared by both modes)
    # -------------------------------------------------------------------------

    def _remove_empty_clauses(self, chapters: List[Chapter]) -> List[Chapter]:
        """Remove clauses with no content at all."""
        for chapter in chapters:
            for section in chapter.sections:
                section.clauses = [
                    cl for cl in section.clauses
                    if cl.content or cl.figures or cl.tables or cl.equations
                ]
        return chapters

    def _merge_continued_tables(self, chapters: List[Chapter]) -> List[Chapter]:
        """Merge multi-page (continued) table fragments."""
        cont_re = re.compile(r'\s*\(continued\)', re.IGNORECASE)

        def _tbl_number_norm(caption: str) -> str:
            cap = cont_re.sub('', caption).strip()
            m = re.match(r'(?:Table\s+)?([\d\.A-Za-z\-]+)', cap, re.IGNORECASE)
            if m:
                return re.sub(r'[.\-\s]', '', m.group(1)).lower()
            return cap.lower()

        for chapter in chapters:
            for section in chapter.sections:
                for clause in section.clauses:
                    if len(clause.tables) <= 1:
                        continue

                    ids_to_remove: set = set()
                    base_map: dict = {}

                    for tbl in clause.tables:
                        cap      = tbl.caption
                        is_cont  = bool(cont_re.search(cap))
                        norm     = _tbl_number_norm(cap)

                        if is_cont:
                            if norm in base_map:
                                base_map[norm].rows.extend(tbl.rows)
                                ids_to_remove.add(tbl.id)
                        else:
                            base_map[norm] = tbl

                    if not ids_to_remove:
                        continue

                    clause.tables = [t for t in clause.tables
                                     if t.id not in ids_to_remove]
                    clause.content = [
                        item for item in clause.content
                        if not (item.type == "table"
                                and item.table_id in ids_to_remove)
                    ]

                    for tbl in clause.tables:
                        if len(tbl.headers) != 2:
                            continue
                        next_val: dict = {}
                        last = ""
                        for idx in range(len(tbl.rows) - 1, -1, -1):
                            v = tbl.rows[idx][1].strip() if len(tbl.rows[idx]) > 1 else ""
                            if v:
                                last = v
                            next_val[idx] = last

                        for idx, row in enumerate(tbl.rows):
                            if len(row) < 2:
                                continue
                            if row[1].strip() or not row[0].strip():
                                continue
                            prev = ""
                            for j in range(idx - 1, -1, -1):
                                pv = tbl.rows[j][1].strip() if len(tbl.rows[j]) > 1 else ""
                                if pv:
                                    prev = pv
                                    break
                            nxt = next_val.get(idx, "")
                            if prev and nxt and prev == nxt:
                                tbl.rows[idx][1] = prev

        return chapters

    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------

    def _make_clause(self, number: str, title: str,
                     page: int, section: Section) -> Clause:
        """Create a new Clause and append it to the given section."""
        cl = Clause(
            id=self._clause_id_for(number),
            number=number, title=title,
            page_span=[page]
        )
        section.clauses.append(cl)
        return cl

    def _clause_id_for(self, number: str) -> str:
        if number:
            return f"CL-{number.replace('.', '-')}"
        self._auto_clause_counter += 1
        return f"CL-AUTO-{self._auto_clause_counter}"

    def _parse_part_heading(self, text: str):
        self._chapter_counter += 1
        m = RE_PART.match(text)
        if m:
            return m.group(1), m.group(2).strip() or text
        return str(self._chapter_counter), text

    def to_dict(self, document: Document) -> dict:
        return asdict(document)


# =============================================================================
# Public entry point
# =============================================================================

def parse_datalab_output(datalab_result: dict, source_pdf: str = "unknown.pdf",
                         figures_dir: str = "storage/figures") -> dict:
    """
    Parse Datalab result -> return JSON-serializable structured document dict.
    Called by main.py.

    Automatically detects whether the PDF is a Part-only document or a full
    multi-Division document and applies the correct parsing logic.
    """
    parser = StructureParser(source_pdf=source_pdf, figures_dir=figures_dir)
    document = parser.parse(datalab_result)
    return parser.to_dict(document)