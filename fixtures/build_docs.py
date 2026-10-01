#!/usr/bin/env python3
"""Build the documentation fixtures: text PDFs (EN + ZH), a valid EPUB,
an image-only (scanned-style) PDF, and a non-audio file.

PDFs are produced with reportlab (selectable text). The scanned PDF is built
by rasterising the EN text PDF to PNG and wrapping the images in a PDF with
Pillow, so it deliberately contains NO text layer.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import DOCS, ROOT  # noqa: E402

NOT_AUDIO = """# not_audio.txt

This file exists to prove that non-audio inputs are handled gracefully.

It is deliberately NOT an audio container: it has a .txt extension and
contains only UTF-8 text. Uploading it to the meeting-transcription API or
the audiobook importer must produce a clear, structured error -- never a
crash and never a silent success.

Lines: 9
Purpose: fixtures/not_audio.txt (task 36)
"""

EN_SECTIONS = [
    (
        "The Coastal Weather Station",
        [
            "The weather station sits on a low cliff above the harbour, where the "
            "wind arrives without anything to slow it down. Every morning at six, "
            "the technicians climb the metal stairs to check the instruments and "
            "record the overnight readings by hand.",
            "The anemometer turns freely in the salt air, and the rain gauge is "
            "cleared of sand before the first measurements of the day are logged. "
            "Visibility is estimated from a set of markers placed along the "
            "breakwater at fixed distances.",
        ],
    ),
    (
        "How the Data Travels",
        [
            "Readings are stored locally on a small computer and then forwarded "
            "to the regional office over a mobile connection. When the signal is "
            "weak, the station keeps the data and retries every few minutes until "
            "the link recovers, so no observation is ever lost.",
            "A second copy is written to a removable card each evening. The card "
            "is collected once a week by a courier who also brings spare parts "
            "and the printed maintenance schedule for the coming days.",
        ],
    ),
    (
        "Why It Matters",
        [
            "Fishermen rely on the station's short-range forecast before leaving "
            "the bay, and the harbour master uses the wind records to decide when "
            "small vessels should stay in port. Accurate, continuous data is the "
            "difference between a routine day and a dangerous one.",
            "Over the years the archive has grown into a detailed picture of how "
            "the local climate is changing, and researchers now visit the station "
            "to compare its long records with satellite measurements.",
        ],
    ),
]

ZH_SECTIONS = [
    (
        "海边的气象站",
        [
            "气象站坐落在海港上方的一道矮崖上，海风毫无遮挡地吹过来。"
            "每天清晨六点，技术人员都会爬上金属楼梯，检查仪器，"
            "并用手工方式记录夜间测得的各项数据。",
            "风速仪在带着咸味的空气里自由转动，雨量计在一天中第一次记录之前"
            "就被清理掉里面的沙子。能见度则是根据防波堤上按固定距离设置的"
            "标记来估算的。",
        ],
    ),
    (
        "数据是怎样传出去的",
        [
            "观测数据先保存在一台小型计算机上，然后通过移动网络转发到地区办公室。"
            "当信号很弱的时候，气象站会先把数据留存下来，每隔几分钟重试一次，"
            "直到连接恢复，因此没有任何一次观测记录会丢失。",
            "每天傍晚，系统还会把一份副本写入可移动的存储卡。"
            "每周都有一名快递员来取卡，同时带来备件和接下来几天的"
            "印刷版维护安排。",
        ],
    ),
    (
        "为什么它很重要",
        [
            "渔民在出海前会参考气象站的短时预报，港务长则根据风的记录"
            "决定小型船只何时应该留在港内。准确而连续的数据，"
            "往往就是平凡的一天和危险的一天之间的差别。",
            "多年来，这些资料已经积累成一幅关于当地气候变化的具体图景，"
            "研究人员如今也会专程来到气象站，把这些长期记录与卫星观测做比较。",
        ],
    ),
]


def build_text_pdf(path: Path, title: str, sections, cjk: bool) -> None:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import cm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer

    body_font = "Helvetica"
    bold_font = "Helvetica-Bold"
    if cjk:
        pdfmetrics.registerFont(
            __import__(
                "reportlab.pdfbase.cidfonts", fromlist=["UnicodeCIDFont"]
            ).UnicodeCIDFont("STSong-Light")
        )
        body_font = bold_font = "STSong-Light"

    styles = getSampleStyleSheet()
    h1 = ParagraphStyle(
        "H1",
        parent=styles["Heading1"],
        fontName=bold_font,
        fontSize=18,
        leading=24,
        spaceAfter=10,
    )
    h2 = ParagraphStyle(
        "H2",
        parent=styles["Heading2"],
        fontName=bold_font,
        fontSize=13,
        leading=18,
        spaceBefore=12,
        spaceAfter=6,
    )
    body = ParagraphStyle(
        "Body",
        parent=styles["BodyText"],
        fontName=body_font,
        fontSize=11,
        leading=17,
        spaceAfter=8,
    )

    doc = SimpleDocTemplate(
        str(path),
        pagesize=A4,
        leftMargin=2 * cm,
        rightMargin=2 * cm,
        topMargin=2 * cm,
        bottomMargin=2 * cm,
        title=title,
        author="VoiceStack fixtures",
    )
    flow = [Paragraph(title, h1), Spacer(1, 6)]
    for i, (heading, paragraphs) in enumerate(sections):
        flow.append(Paragraph(heading, h2))
        for p in paragraphs:
            flow.append(Paragraph(p, body))
        if i < len(sections) - 1:
            flow.append(PageBreak())
    doc.build(flow)


def build_epub(path: Path) -> None:
    title = "Two Short Chapters"
    author = "VoiceStack Fixtures"
    uid = "urn:uuid:6f1c2d3e-voicestack-fixture-epub"

    container = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<container version="1.0" '
        'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
        "  <rootfiles>\n"
        '    <rootfile full-path="OEBPS/content.opf" '
        'media-type="application/oebps-package+xml"/>\n'
        "  </rootfiles>\n"
        "</container>\n"
    )
    opf = f"""<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="bookid">{uid}</dc:identifier>
    <dc:title>{title}</dc:title>
    <dc:language>en</dc:language>
    <dc:creator>{author}</dc:creator>
    <meta property="dcterms:modified">2026-10-01T00:00:00Z</meta>
  </metadata>
  <manifest>
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
    <item id="c1" href="chapter1.xhtml" media-type="application/xhtml+xml"/>
    <item id="c2" href="chapter2.xhtml" media-type="application/xhtml+xml"/>
    <item id="c3" href="chapter3.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine>
    <itemref idref="c1"/>
    <itemref idref="c2"/>
    <itemref idref="c3"/>
  </spine>
</package>
"""
    nav = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="en">
<head><meta charset="utf-8"/><title>{title}</title></head>
<body>
  <nav epub:type="toc" id="toc">
    <h1>Contents</h1>
    <ol>
      <li><a href="chapter1.xhtml">Chapter 1: The Harbour Light</a></li>
      <li><a href="chapter2.xhtml">Chapter 2: The Long Winter</a></li>
      <li><a href="chapter3.xhtml">Chapter 3: The First Boat Home</a></li>
    </ol>
  </nav>
</body>
</html>
"""

    def chapter(num: int, heading: str, paras: list[str]) -> str:
        body = "\n".join(f"    <p>{p}</p>" for p in paras)
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" lang="en">
<head><meta charset="utf-8"/><title>{heading}</title></head>
<body>
  <section>
    <h1>Chapter {num}: {heading}</h1>
{body}
  </section>
</body>
</html>
"""

    chapters = {
        "OEBPS/chapter1.xhtml": chapter(
            1,
            "The Harbour Light",
            [
                "The light had burned on the headland for a hundred years, and the "
                "keeper knew every stone of the tower by heart.",
                "On clear nights the beam could be seen from the far side of the "
                "bay, sweeping slowly across the water like a patient hand.",
            ],
        ),
        "OEBPS/chapter2.xhtml": chapter(
            2,
            "The Long Winter",
            [
                "That year the storms came early, and the road to the village "
                "disappeared under drifts of snow for almost three months.",
                "Supplies arrived by boat when the sea allowed it, and the "
                "villagers learned to count their fuel in careful, even measures.",
            ],
        ),
        "OEBPS/chapter3.xhtml": chapter(
            3,
            "The First Boat Home",
            [
                "When the ice finally broke, the first boat came in low and slow, "
                "its crew waving long before anyone could hear them shout.",
                "The light turned again that evening, and the harbour was full.",
            ],
        ),
    }

    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        # mimetype MUST be first and uncompressed.
        z.writestr(
            zipfile.ZipInfo("mimetype"),
            "application/epub+zip",
            compress_type=zipfile.ZIP_STORED,
        )
        z.writestr("META-INF/container.xml", container)
        z.writestr("OEBPS/content.opf", opf)
        z.writestr("OEBPS/nav.xhtml", nav)
        for name, content in chapters.items():
            z.writestr(name, content)


def build_scanned_pdf(text_pdf: Path, path: Path) -> None:
    from PIL import Image

    with tempfile.TemporaryDirectory() as td:
        prefix = Path(td) / "page"
        subprocess.run(
            ["pdftoppm", "-r", "150", "-png", str(text_pdf), str(prefix)], check=True
        )
        pages = sorted(Path(td).glob("page-*.png"))
        if not pages:
            raise SystemExit("pdftoppm produced no pages")
        images = [Image.open(p).convert("RGB") for p in pages]
        path.parent.mkdir(parents=True, exist_ok=True)
        images[0].save(
            path, "PDF", resolution=150.0, save_all=True, append_images=images[1:]
        )


def main() -> int:
    DOCS.mkdir(parents=True, exist_ok=True)

    en_pdf = DOCS / "en_text.pdf"
    zh_pdf = DOCS / "zh_text.pdf"
    build_text_pdf(en_pdf, "The Coastal Weather Station", EN_SECTIONS, cjk=False)
    build_text_pdf(zh_pdf, "海边的气象站", ZH_SECTIONS, cjk=True)

    build_epub(DOCS / "book.epub")
    build_scanned_pdf(en_pdf, DOCS / "scanned.pdf")

    (ROOT / "not_audio.txt").write_text(NOT_AUDIO, encoding="utf-8")

    for p in [
        en_pdf,
        zh_pdf,
        DOCS / "book.epub",
        DOCS / "scanned.pdf",
        ROOT / "not_audio.txt",
    ]:
        print(f"  {p.relative_to(ROOT)}  {p.stat().st_size} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
