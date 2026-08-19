from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def sample_pdf(tmp_path: Path) -> Path:
    import pymupdf

    path = tmp_path / "sample.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((58, 70), "Quarterly editorial report", fontsize=22, fontname="helv")
    page.insert_textbox(
        pymupdf.Rect(58, 110, 530, 220),
        "This document tests the complete translation workflow.\nIt includes two paragraphs and a page number.",
        fontsize=12,
        fontname="helv",
    )
    page.insert_text((278, 810), "1", fontsize=10, fontname="helv")
    doc.save(path)
    doc.close()
    return path
