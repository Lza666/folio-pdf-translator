from __future__ import annotations

import html
import json
import re
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from typing import Any, Literal


class StructureValidationError(ValueError):
    pass


@dataclass(slots=True)
class InlineRun:
    id: str
    source_text: str
    bbox: list[float]
    font_name: str | None = None
    font_size: float | None = None
    font_color: int | None = None
    bold: bool = False
    italic: bool = False
    target_text: str | None = None


@dataclass(slots=True)
class Paragraph:
    id: str
    bbox: list[float]
    runs: list[InlineRun] = field(default_factory=list)
    type: Literal["paragraph"] = "paragraph"


@dataclass(slots=True)
class ListItem:
    id: str
    bbox: list[float]
    runs: list[InlineRun] = field(default_factory=list)
    marker: str = "•"
    marker_bbox: list[float] | None = None
    marker_drawn: bool = False
    content_indent: float = 0.0
    type: Literal["list_item"] = "list_item"


DocumentElement = Paragraph | ListItem


@dataclass(slots=True)
class SegmentStructure:
    elements: list[DocumentElement]
    version: int = 1


def structure_to_json(structure: SegmentStructure) -> str:
    return json.dumps(asdict(structure), ensure_ascii=False, separators=(",", ":"))


def structure_from_json(value: str | None) -> SegmentStructure | None:
    if not value:
        return None
    try:
        payload = json.loads(value)
        elements: list[DocumentElement] = []
        for raw_element in payload.get("elements", []):
            runs = [InlineRun(**raw_run) for raw_run in raw_element.get("runs", [])]
            common: dict[str, Any] = {
                "id": raw_element["id"],
                "bbox": raw_element["bbox"],
                "runs": runs,
            }
            if raw_element.get("type") == "list_item":
                elements.append(
                    ListItem(
                        **common,
                        marker=raw_element.get("marker", "•"),
                        marker_bbox=raw_element.get("marker_bbox"),
                        marker_drawn=bool(raw_element.get("marker_drawn", False)),
                        content_indent=float(raw_element.get("content_indent", 0.0)),
                    )
                )
            else:
                elements.append(Paragraph(**common))
        if not elements:
            return None
        return SegmentStructure(elements=elements, version=int(payload.get("version", 1)))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise StructureValidationError("片段结构数据无效") from exc


def element_source_text(element: DocumentElement) -> str:
    return "".join(run.source_text for run in element.runs).strip()


def element_target_text(element: DocumentElement) -> str:
    return "".join((run.target_text or "") for run in element.runs).strip()


def structure_source_text(structure: SegmentStructure) -> str:
    return "\n".join(element_source_text(element) for element in structure.elements).strip()


def structure_target_text(structure: SegmentStructure) -> str:
    return "\n".join(element_target_text(element) for element in structure.elements).strip()


def encode_structure_for_translation(structure_json: str | None) -> str | None:
    structure = structure_from_json(structure_json)
    if structure is None:
        return None
    root = ET.Element("folio", {"version": str(structure.version)})
    for element in structure.elements:
        tag = "list-item" if isinstance(element, ListItem) else "paragraph"
        node = ET.SubElement(root, tag, {"id": element.id})
        for run in element.runs:
            run_node = ET.SubElement(
                node,
                "run",
                {
                    "id": run.id,
                    "bold": "1" if run.bold else "0",
                    "italic": "1" if run.italic else "0",
                },
            )
            run_node.text = run.source_text
    return ET.tostring(root, encoding="unicode", short_empty_elements=False)


def apply_structured_translation(
    structure_json: str | None, translated_xml: str
) -> tuple[str, str]:
    structure = structure_from_json(structure_json)
    if structure is None:
        raise StructureValidationError("片段没有可更新的结构")
    value = translated_xml.strip()
    if value.startswith("```"):
        value = value.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    start, end = value.find("<folio"), value.rfind("</folio>")
    if start < 0 or end < start:
        raise StructureValidationError("模型未返回完整的 folio 结构")
    try:
        root = ET.fromstring(value[start : end + len("</folio>")])
    except ET.ParseError as exc:
        raise StructureValidationError("模型返回的 folio 结构无法解析") from exc

    source_elements = {element.id: element for element in structure.elements}
    returned_elements = list(root)
    if [node.attrib.get("id") for node in returned_elements] != list(source_elements):
        raise StructureValidationError("模型改变了段落或列表项的 ID/顺序")
    for node in returned_elements:
        element = source_elements[node.attrib["id"]]
        expected_tag = "list-item" if isinstance(element, ListItem) else "paragraph"
        if node.tag != expected_tag:
            raise StructureValidationError("模型改变了段落或列表项类型")
        expected_runs = {run.id: run for run in element.runs}
        returned_runs = list(node)
        if [run.attrib.get("id") for run in returned_runs] != list(expected_runs):
            raise StructureValidationError("模型改变了行内样式片段的 ID/顺序")
        for run_node in returned_runs:
            run = expected_runs[run_node.attrib["id"]]
            if run_node.tag != "run":
                raise StructureValidationError("模型返回了未知的行内结构")
            translated = "".join(run_node.itertext()).strip()
            if run.source_text.strip() and not translated:
                raise StructureValidationError(f"模型漏译了行内片段：{run.id}")
            run.target_text = translated

    target_text = structure_target_text(structure)
    if not target_text:
        raise StructureValidationError("模型返回的结构化译文为空")
    return target_text, structure_to_json(structure)


def copy_source_structure_to_target(structure_json: str | None) -> str | None:
    structure = structure_from_json(structure_json)
    if structure is None:
        return structure_json
    for element in structure.elements:
        for run in element.runs:
            run.target_text = run.source_text
    return structure_to_json(structure)


def _normalized_layout_text(value: str) -> str:
    return "\n".join(line.strip() for line in value.splitlines() if line.strip())


def target_structure_matches(structure_json: str | None, target_text: str | None) -> bool:
    structure = structure_from_json(structure_json)
    if structure is None or not target_text:
        return False
    if any(run.target_text is None for element in structure.elements for run in element.runs):
        return False
    return _normalized_layout_text(structure_target_text(structure)) == _normalized_layout_text(
        target_text
    )


def structure_as_api_value(structure_json: str | None) -> dict[str, Any] | None:
    structure = structure_from_json(structure_json)
    return asdict(structure) if structure else None


def structured_target_html(structure_json: str | None, target_text: str | None) -> str | None:
    if not target_structure_matches(structure_json, target_text):
        return None
    structure = structure_from_json(structure_json)
    if structure is None:
        return None
    parts = ['<div class="folio-root">']
    for element in structure.elements:
        classes = "folio-list-item" if isinstance(element, ListItem) else "folio-paragraph"
        indent = element.content_indent if isinstance(element, ListItem) else 0.0
        style = f"padding-left:{indent:.3f}pt" if indent > 0 else ""
        parts.append(f'<div class="{classes}" style="{style}">')
        if isinstance(element, ListItem) and not element.marker_drawn:
            parts.append(f'<span class="folio-marker">{html.escape(element.marker)}</span>')
        for run in element.runs:
            run_classes = []
            if run.bold:
                run_classes.append("folio-bold")
            if run.italic:
                run_classes.append("folio-italic")
            class_attr = f' class="{" ".join(run_classes)}"' if run_classes else ""
            parts.append(f"<span{class_attr}>{html.escape(run.target_text or '')}</span>")
        parts.append("</div>")
    parts.append("</div>")
    return "".join(parts)


def plain_text_from_translated_xml(value: str) -> str:
    """Useful for diagnostics without exposing structural markup to editors."""
    try:
        root = ET.fromstring(value)
    except ET.ParseError:
        return re.sub(r"<[^>]+>", "", value).strip()
    return "\n".join("".join(node.itertext()).strip() for node in root).strip()
