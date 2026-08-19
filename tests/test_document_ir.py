from __future__ import annotations

import pytest

from app.document_ir import (
    StructureValidationError,
    apply_structured_translation,
    encode_structure_for_translation,
    structure_from_json,
    structured_target_html,
    target_structure_matches,
)
from app.services.pdf import _native_block_units


def _resume_list_block() -> dict:
    return {
        "bbox": [24.0, 48.0, 280.0, 96.0],
        "lines": [
            {
                "bbox": [38.0, 48.0, 272.0, 60.0],
                "spans": [
                    {
                        "text": "数据治理：",
                        "bbox": [38.0, 48.0, 88.0, 60.0],
                        "font": "Source-Bold",
                        "size": 10.0,
                        "flags": 16,
                        "color": 0,
                    },
                    {
                        "text": "构建统一语义层",
                        "bbox": [88.0, 48.0, 180.0, 60.0],
                        "font": "Source-Regular",
                        "size": 10.0,
                        "flags": 0,
                        "color": 0,
                    },
                ],
            },
            {
                "bbox": [38.0, 60.0, 190.0, 72.0],
                "spans": [
                    {
                        "text": "并完成字段标准化。",
                        "bbox": [38.0, 60.0, 190.0, 72.0],
                        "font": "Source-Regular",
                        "size": 10.0,
                        "flags": 0,
                        "color": 0,
                    }
                ],
            },
            {
                "bbox": [38.0, 76.0, 260.0, 88.0],
                "spans": [
                    {
                        "text": "缓存设计：",
                        "bbox": [38.0, 76.0, 88.0, 88.0],
                        "font": "Source-Bold",
                        "size": 10.0,
                        "flags": 16,
                        "color": 0,
                    },
                    {
                        "text": "减少外部接口调用。",
                        "bbox": [88.0, 76.0, 220.0, 88.0],
                        "font": "Source-Regular",
                        "size": 10.0,
                        "flags": 0,
                        "color": 0,
                    },
                ],
            },
        ],
    }


def test_native_list_items_and_inline_styles_are_preserved():
    markers = [[25.0, 51.0, 29.0, 55.0], [25.0, 79.0, 29.0, 83.0]]
    units = _native_block_units(
        _resume_list_block(), page_width=300.0, page_height=180.0, markers=markers
    )

    assert len(units) == 2
    assert [unit[3] for unit in units] == ["list_item", "list_item"]
    first = structure_from_json(units[0][4])
    assert first is not None
    assert first.elements[0].type == "list_item"
    assert first.elements[0].runs[0].bold is True
    assert first.elements[0].runs[1].source_text.endswith("并完成字段标准化。")
    assert first.elements[0].content_indent == 13.0


def test_structured_translation_round_trip_preserves_ids_and_styles():
    markers = [[25.0, 51.0, 29.0, 55.0], [25.0, 79.0, 29.0, 83.0]]
    structure_json = _native_block_units(
        _resume_list_block(), page_width=300.0, page_height=180.0, markers=markers
    )[0][4]
    payload = encode_structure_for_translation(structure_json)
    assert payload is not None
    assert 'bold="1"' in payload

    translated = (
        '<folio version="1"><list-item id="e0">'
        '<run id="e0-r0" bold="1" italic="0">Data Governance:</run>'
        '<run id="e0-r1" bold="0" italic="0"> Built a unified semantic layer and standardized fields.</run>'
        "</list-item></folio>"
    )
    target_text, updated = apply_structured_translation(structure_json, translated)

    assert target_text.startswith("Data Governance:")
    assert target_structure_matches(updated, target_text)
    rich_html = structured_target_html(updated, target_text)
    assert rich_html is not None
    assert "folio-bold" in rich_html
    assert "padding-left:13.000pt" in rich_html
    assert "folio-marker" not in rich_html


def test_structured_translation_rejects_missing_inline_run():
    markers = [[25.0, 51.0, 29.0, 55.0], [25.0, 79.0, 29.0, 83.0]]
    structure_json = _native_block_units(
        _resume_list_block(), page_width=300.0, page_height=180.0, markers=markers
    )[0][4]

    with pytest.raises(StructureValidationError, match="ID/顺序"):
        apply_structured_translation(
            structure_json,
            '<folio version="1"><list-item id="e0">'
            '<run id="e0-r0" bold="1" italic="0">Data Governance:</run>'
            "</list-item></folio>",
        )
