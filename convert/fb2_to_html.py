#!/usr/bin/env python3
import argparse
import html
import re
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

FB_NS = "http://www.gribuser.ru/xml/fictionbook/2.0"
XLINK_NS = "http://www.w3.org/1999/xlink"
XML_NS = "http://www.w3.org/XML/1998/namespace"

NS = {"fb": FB_NS, "xlink": XLINK_NS}


def local_name(tag: str) -> str:
    return tag.split("}", 1)[-1]


def read_fb2(path: Path) -> bytes:
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as z:
            names = [n for n in z.namelist() if n.lower().endswith(".fb2")]
            if not names:
                raise ValueError("Keine .fb2-Datei im ZIP gefunden.")
            return z.read(names[0])
    return path.read_bytes()


def plain_text(elem) -> str:
    return " ".join("".join(elem.itertext()).split()) if elem is not None else ""


def xml_id(elem) -> str:
    return elem.attrib.get(f"{{{XML_NS}}}id", "") or elem.attrib.get("id", "")


def extract_binaries(root):
    binaries = {}

    for b in root.findall(".//fb:binary", NS):
        img_id = b.attrib.get("id")
        content_type = b.attrib.get("content-type", "application/octet-stream")
        data = re.sub(r"\s+", "", b.text or "")

        if img_id and data:
            binaries[img_id] = f"data:{content_type};base64,{data}"

    return binaries


def extract_description(root):
    title_info = root.find(".//fb:description/fb:title-info", NS)
    doc_info = root.find(".//fb:description/fb:document-info", NS)

    def get(path, base=title_info):
        node = base.find(path, NS) if base is not None else None
        return plain_text(node)

    authors = []

    if title_info is not None:
        for a in title_info.findall("fb:author", NS):
            first = plain_text(a.find("fb:first-name", NS))
            middle = plain_text(a.find("fb:middle-name", NS))
            last = plain_text(a.find("fb:last-name", NS))
            nickname = plain_text(a.find("fb:nickname", NS))

            name = " ".join(x for x in [first, middle, last] if x).strip()
            if not name:
                name = nickname

            if name:
                authors.append(name)

    genres = []
    if title_info is not None:
        genres = [plain_text(g) for g in title_info.findall("fb:genre", NS)]

    meta = {
        "Titel": get("fb:book-title"),
        "Autor": ", ".join(authors),
        "Genre": ", ".join(g for g in genres if g),
        "Sprache": get("fb:lang"),
        "Datum": get("fb:date"),
        "Serie": "",
        "Dokument-ID": "",
        "Version": "",
    }

    sequence = title_info.find("fb:sequence", NS) if title_info is not None else None
    if sequence is not None:
        name = sequence.attrib.get("name", "")
        number = sequence.attrib.get("number", "")

        if name and number:
            meta["Serie"] = f"{name} #{number}"
        elif name:
            meta["Serie"] = name

    if doc_info is not None:
        meta["Dokument-ID"] = plain_text(doc_info.find("fb:id", NS))
        meta["Version"] = plain_text(doc_info.find("fb:version", NS))

    annotation = title_info.find("fb:annotation", NS) if title_info is not None else None

    coverpage = None

    if title_info is not None:
        coverpage = title_info.find("fb:coverpage", NS)

    return meta, annotation, coverpage

def render_inline(elem, binaries, note_backrefs=None, current_anchor=None):
    tag = local_name(elem.tag)

    text = html.escape(elem.text or "")
    children = "".join(
        render_inline(child, binaries, note_backrefs, current_anchor)
        for child in elem
    )
    tail = html.escape(elem.tail or "")

    if tag == "strong":
        return f"<strong>{text}{children}</strong>{tail}"

    if tag == "emphasis":
        return f"<em>{text}{children}</em>{tail}"

    if tag == "strikethrough":
        return f"<s>{text}{children}</s>{tail}"

    if tag == "sub":
        return f"<sub>{text}{children}</sub>{tail}"

    if tag == "sup":
        return f"<sup>{text}{children}</sup>{tail}"

    if tag == "code":
        return f"<code>{text}{children}</code>{tail}"

    if tag == "a":
        href = elem.attrib.get(f"{{{XLINK_NS}}}href", "#")

        if href.startswith("#"):
            note_id = href[1:]
            note_target = "note-" + note_id

            if note_backrefs is not None and current_anchor:
                note_backrefs.setdefault(note_id, [])
                if current_anchor not in note_backrefs[note_id]:
                    note_backrefs[note_id].append(current_anchor)

            return (
                f'<a href="#{html.escape(note_target)}" '
                f'class="note-ref">{text}{children}</a>{tail}'
            )

        return f'<a href="{html.escape(href)}">{text}{children}</a>{tail}'

    if tag == "image":
        href = elem.attrib.get(f"{{{XLINK_NS}}}href", "")
        img_id = href.lstrip("#")
        src = binaries.get(img_id, img_id)

        return (
            f'<img src="{html.escape(src)}" '
            f'alt="{html.escape(img_id)}" />{tail}'
        )

    return f"{text}{children}{tail}"

def build_toc(elem, binaries, toc_items, section_counter, level=1):
    if local_name(elem.tag) != "section":
        for child in elem:
            build_toc(child, binaries, toc_items, section_counter, level)
        return

    section_counter["value"] += 1
    section_id = f"section-{section_counter['value']}"
    elem.attrib["_html_id"] = section_id

    title_elem = elem.find("fb:title", NS)
    title_text = plain_text(title_elem) if title_elem is not None else ""

    if title_text:
        toc_items.append({
            "level": level,
            "id": section_id,
            "title": title_text,
        })

    for child in elem:
        if local_name(child.tag) == "section":
            build_toc(
                child,
                binaries,
                toc_items,
                section_counter,
                level + 1
            )

def render_block(
    elem,
    binaries,
    heading_level=2,
    note_backrefs=None,
    in_notes=False,
    anchor_counter=None,
):
    tag = local_name(elem.tag)
    elem_id = xml_id(elem)

    if tag == "section":
        if in_notes and elem_id:
            html_id = "note-" + elem_id
        else:
            html_id = elem.attrib.get("_html_id") or elem_id

        id_attr = f' id="{html.escape(html_id)}"' if html_id else ""

        back_link = ""
        note_number = ""

        if in_notes:
            title_elem = elem.find("fb:title", NS)
            if title_elem is not None:
                note_number = plain_text(title_elem)

        if in_notes and elem_id:
            refs = note_backrefs.get(elem_id, []) if note_backrefs else []

            if refs:
                links = " ".join(
                    f'<a href="#{html.escape(ref)}" class="note-backref">'
                    f'<span class="note-number">{html.escape(note_number)}</span> ↩'
                    f"</a>"
                    for ref in refs
                )
                back_link = f'<p class="note-backlinks">{links}</p>\n'

        children = []

        for child in elem:
            if in_notes and local_name(child.tag) == "title":
                continue

            children.append(
                render_block(
                    child,
                    binaries,
                    heading_level + 1,
                    note_backrefs,
                    in_notes,
                    anchor_counter,
                )
            )

        return (
            f"<section{id_attr}>\n"
            + back_link
            + "".join(children)
            + "</section>\n"
        )

    html_id = elem.attrib.get("_html_id") or elem_id
    id_attr = f' id="{html.escape(html_id)}"' if html_id else ""

    if tag == "title":
        text = plain_text(elem)
        level = min(heading_level, 6)
        return f"<h{level}>{html.escape(text)}</h{level}>\n" if text else ""

    if tag == "subtitle":
        return f"<h4>{render_inline(elem, binaries, note_backrefs, html_id)}</h4>\n"

    if tag == "p":
        if not html_id and not in_notes:
            if anchor_counter is not None:
                anchor_counter["value"] += 1
                html_id = f"ref-{anchor_counter['value']}"
            else:
                html_id = f"ref-{id(elem)}"

            id_attr = f' id="{html.escape(html_id)}"'

        return (
            f"<p{id_attr}>"
            f"{render_inline(elem, binaries, note_backrefs, html_id)}"
            f"</p>\n"
        )

    if tag == "empty-line":
        return "<br />\n"

    if tag == "epigraph":
        return (
            "<blockquote>\n"
            + "".join(
                render_block(child, binaries, heading_level, note_backrefs, in_notes, anchor_counter)
                for child in elem
            )
            + "</blockquote>\n"
        )

    if tag == "cite":
        return (
            '<blockquote class="cite">\n'
            + "".join(
                render_block(child, binaries, heading_level, note_backrefs, in_notes, anchor_counter)
                for child in elem
            )
            + "</blockquote>\n"
        )

    if tag == "poem":
        lines = []

        for stanza in elem:
            for v in stanza:
                if local_name(v.tag) == "v":
                    lines.append(plain_text(v))
            lines.append("")

        return '<pre class="poem">' + html.escape("\n".join(lines).strip()) + "</pre>\n"

    if tag == "image":
        return render_inline(elem, binaries, note_backrefs, html_id) + "\n"

    return "".join(
        render_block(child, binaries, heading_level, note_backrefs, in_notes, anchor_counter)
        for child in elem
    )


def render_description(meta, annotation, binaries):
    rows = []

    for key, value in meta.items():
        if value:
            rows.append(
                f"<tr><th>{html.escape(key)}</th>"
                f"<td>{html.escape(value)}</td></tr>"
            )

    result = '<section class="description">\n<h2>Metadaten</h2>\n'

    if rows:
        result += "<table>\n" + "\n".join(rows) + "\n</table>\n"

    if annotation is not None:
        result += "<h3>Annotation</h3>\n"

        for child in annotation:
            result += render_block(child, binaries, 4)

    result += "</section>\n"
    return result


def render_notes(root, binaries, note_backrefs):
    notes_bodies = root.findall(".//fb:body[@name='notes']", NS)

    if not notes_bodies:
        return ""

    html_parts = ['<section class="notes">\n<h2>Notizen</h2>\n']

    for body in notes_bodies:
        for child in body:
            html_parts.append(
                render_block(
                    child,
                    binaries,
                    3,
                    note_backrefs,
                    in_notes=True,
                )
            )

    html_parts.append("</section>\n")
    return "".join(html_parts)

def render_coverpage(coverpage, binaries):
    if coverpage is None:
        return ""

    html_parts = ['<section class="coverpage">\n']

    for child in coverpage:
        if local_name(child.tag) == "image":
            html_parts.append(render_inline(child, binaries))

    html_parts.append("</section>\n")

    return "".join(html_parts)

def fb2_to_html(input_path: Path, output_path: Path):
    root = ET.fromstring(read_fb2(input_path))

    binaries = extract_binaries(root)
    meta, annotation, coverpage = extract_description(root)
    title = meta.get("Titel") or input_path.stem

    note_backrefs = {}
    anchor_counter = {"value": 0}

    normal_bodies = [
        b for b in root.findall("fb:body", NS)
        if b.attrib.get("name") != "notes"
    ]

    #
    # Inhaltsverzeichnis erzeugen
    #
    toc_items = []
    section_counter = {"value": 0}

    for body in normal_bodies:
        for child in body:
            build_toc(
                child,
                binaries,
                toc_items,
                section_counter
            )

    toc_html = ""

    if toc_items:
        toc_html = """
    <nav class="toc">
    <h2>Inhaltsverzeichnis</h2>
    <ul>
    """

        for item in toc_items:
            toc_html += (
                f'<li class="toc-level-{item["level"]}">'
                f'<a href="#{html.escape(item["id"])}">'
                f'{html.escape(item["title"])}'
                f'</a>'
                f'</li>\n'
            )

        toc_html += """
    </ul>
    </nav>
    """

    #
    # Haupttext rendern
    #
    main_html = ""

    for body in normal_bodies:
        main_html += "<main>\n"

        for child in body:
            main_html += render_block(
                child,
                binaries,
                2,
                note_backrefs,
                False,
                anchor_counter,
            )

        main_html += "</main>\n"

    notes_html = render_notes(root, binaries, note_backrefs)

    html_doc = f"""<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<title>{html.escape(title)}</title>
<style>
body {{
    max-width: 820px;
    margin: 3rem auto;
    padding: 0 1rem;
    font-family: Georgia, serif;
    line-height: 1.6;
}}

table {{
    border-collapse: collapse;
    margin-bottom: 2rem;
}}

th {{
    text-align: left;
    padding-right: 1rem;
    vertical-align: top;
}}

td, th {{
    border-bottom: 1px solid #ddd;
    padding: .35rem .75rem .35rem 0;
}}

img {{
    max-width: 100%;
}}

blockquote {{
    margin-left: 1.5rem;
    padding-left: 1rem;
    border-left: 3px solid #ccc;
}}

pre.poem {{
    font-family: Georgia, serif;
    white-space: pre-wrap;
}}

.note-ref {{
    text-decoration: none;
    vertical-align: super;
    font-size: 0.85em;
}}

.note-backlinks {{
    font-size: 0.9em;
    margin-bottom: 0.5rem;
}}

.note-backref {{
    text-decoration: none;
}}

.notes {{
    margin-top: 4rem;
    border-top: 1px solid #aaa;
}}

.coverpage {{
    text-align: center;
    margin-bottom: 2rem;
}}

.coverpage img {{
    max-width: 100%;
    max-height: 80vh;
}}

.note-number {{
    font-weight: bold;
    font-size: 1.1em;
    margin-right: 0.3em;
}}

.toc {{
    margin: 2rem 0 3rem 0;
    padding: 1rem;
    border: 1px solid #ddd;
}}

.toc ul {{
    list-style: none;
    padding-left: 0;
}}

.toc li {{
    margin: 0.25rem 0;
}}

.toc a {{
    text-decoration: none;
}}

.toc-level-1 {{
    font-weight: bold;
}}

.toc-level-2 {{
    margin-left: 1.5rem;
}}

.toc-level-3 {{
    margin-left: 3rem;
}}

.toc-level-4 {{
    margin-left: 4.5rem;
}}
</style>
</head>
<body>
<h1>{html.escape(title)}</h1>

{render_coverpage(coverpage, binaries)}

{render_description(meta, annotation, binaries)}

{toc_html}

{main_html}

{notes_html}

</body>
</html>
"""

    output_path.write_text(html_doc, encoding="utf-8")


def html_to_pdf(html_path: Path, pdf_path: Path):
    try:
        from weasyprint import HTML
    except ImportError as exc:
        raise RuntimeError(
            "PDF-Konvertierung benötigt WeasyPrint. "
            "Installiere es mit: pip install weasyprint"
        ) from exc

    HTML(filename=str(html_path)).write_pdf(str(pdf_path))


def main():
    parser = argparse.ArgumentParser(
        description="Konvertiert FB2/FB2.ZIP nach HTML oder PDF."
    )
    parser.add_argument("input", help="Eingabedatei, z.B. buch.fb2 oder buch.fb2.zip")
    parser.add_argument("-o", "--output", help="Ausgabedatei, z.B. buch.html oder buch.pdf")
    parser.add_argument("--pdf", action="store_true", help="PDF statt HTML erzeugen")

    args = parser.parse_args()

    input_path = Path(args.input)

    if not input_path.exists():
        raise FileNotFoundError(input_path)

    if args.output:
        output_path = Path(args.output)
    else:
        output_path = input_path.with_suffix(".pdf" if args.pdf else ".html")

    if args.pdf:
        html_path = output_path.with_suffix(".html")
        fb2_to_html(input_path, html_path)
        html_to_pdf(html_path, output_path)
        print(f"PDF erstellt: {output_path}")
    else:
        fb2_to_html(input_path, output_path)
        print(f"HTML erstellt: {output_path}")


if __name__ == "__main__":
    main()
