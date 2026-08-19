from __future__ import annotations

import hashlib
import io
import json
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import pymupdf
from fontTools.ttLib import TTFont
from fontTools.varLib.instancer import instantiateVariableFont
from PIL import Image
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.document_ir import structured_target_html
from app.models import Artifact, Job, Page, QualityIssue, Segment
from app.security import secret_store
from app.services.quality import add_issue, unresolved_count

FONT_CANDIDATES = {
    "zh-Hans": ["NotoSansSC-VF.ttf", "msyh.ttc", "simhei.ttf"],
    "zh-Hant": ["msjh.ttc", "NotoSansSC-VF.ttf"],
    "ja": ["YuGothM.ttc", "NotoSansSC-VF.ttf"],
    "ko": ["malgun.ttf", "NotoSansSC-VF.ttf"],
    "latin": ["NotoSans-Regular.ttf", "arial.ttf"],
}


class FinalQualityGateError(ValueError):
    pass


@dataclass(slots=True)
class BackgroundRepair:
    fill: tuple[float, float, float] | bool
    patch_rect: pymupdf.Rect | None = None
    patch_png: bytes | None = None


@dataclass(slots=True)
class PreparedTranslation:
    segment: Segment
    rect: pymupdf.Rect
    font_size: float
    repair: BackgroundRepair


@dataclass(slots=True)
class CompressionCandidate:
    segment: Segment
    max_characters: int


def _font_file(job: Job) -> Path | None:
    override = os.environ.get("FOLIO_FONT_PATH")
    if override and Path(override).is_file():
        return _regular_font_instance(Path(override))
    font_dir = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    group = job.target_language if job.target_language in FONT_CANDIDATES else "latin"
    selected = next(
        (font_dir / name for name in FONT_CANDIDATES[group] if (font_dir / name).is_file()),
        None,
    )
    return _regular_font_instance(selected) if selected else None


@lru_cache(maxsize=16)
def _regular_font_instance(font_file: Path) -> Path:
    """Pin variable fonts to regular weight instead of using their often-thin default."""
    try:
        font = TTFont(font_file, lazy=False)
        if "fvar" not in font or not any(axis.axisTag == "wght" for axis in font["fvar"].axes):
            font.close()
            return font_file

        cache_dir = get_settings().data_dir / "fonts"
        cache_dir.mkdir(parents=True, exist_ok=True)
        fingerprint = hashlib.sha256(
            f"{font_file.resolve()}:{font_file.stat().st_mtime_ns}:400-v2".encode()
        ).hexdigest()[:12]
        output = cache_dir / f"{font_file.stem}-{fingerprint}-w400.ttf"
        if output.is_file():
            font.close()
            return output

        regular = instantiateVariableFont(font, {"wght": 400}, inplace=False)
        names = regular["name"]
        family = names.getDebugName(1) or font_file.stem
        postscript_family = "".join(character for character in family if character.isalnum())
        replacements = {
            2: "Regular",
            4: f"{family} Regular",
            6: f"{postscript_family}-Regular",
            17: "Regular",
        }
        for name_id, replacement in replacements.items():
            for record in [item for item in names.names if item.nameID == name_id]:
                names.setName(
                    replacement,
                    name_id,
                    record.platformID,
                    record.platEncID,
                    record.langID,
                )
        temporary = output.with_suffix(".tmp")
        regular.save(temporary)
        regular.close()
        font.close()
        os.replace(temporary, output)
        return output
    except (KeyError, OSError, TypeError, ValueError):
        return font_file


def _color_from_int(value: int | None) -> tuple[float, float, float]:
    color = value or 0
    return ((color >> 16 & 255) / 255, (color >> 8 & 255) / 255, (color & 255) / 255)


def _layout_text(segment: Segment) -> str:
    """Let the destination box choose line breaks instead of trusting model formatting."""
    text = segment.target_text or ""
    return " ".join(line.strip() for line in text.splitlines() if line.strip())


def _html_color(value: int | None) -> str:
    red, green, blue = (round(channel * 255) for channel in _color_from_int(value))
    return f"#{red:02x}{green:02x}{blue:02x}"


def _rich_text_css(segment: Segment, font_file: Path | None, font_size: float) -> tuple[str, object | None]:
    archive = None
    font_rule = ""
    family = "sans-serif"
    if font_file:
        archive = pymupdf.Archive(str(font_file.parent))
        family = "FolioRich"
        font_rule = (
            f"@font-face {{ font-family: FolioRich; src: url('{font_file.name}'); }}"
        )
    css = (
        f"{font_rule}"
        "* { margin: 0; padding: 0; }"
        f".folio-root {{ font-family: {family}; font-size: {font_size:.3f}pt; "
        f"line-height: 0.92; color: {_html_color(segment.font_color)}; }}"
        ".folio-root div { margin: 0; padding-top: 0; padding-bottom: 0; }"
        ".folio-bold { font-weight: 700; }"
        ".folio-italic { font-style: italic; }"
        ".folio-marker { display: inline-block; width: 1.15em; margin-left: -1.15em; }"
    )
    return css, archive


def _insert_rich_textbox(
    page,
    rect: pymupdf.Rect,
    segment: Segment,
    font_file: Path | None,
    font_size: float,
    *,
    overlay: bool,
) -> float | None:
    rich_html = structured_target_html(segment.structure_json, segment.target_text)
    if rich_html is None:
        return None
    css, archive = _rich_text_css(segment, font_file, font_size)
    spare_height, _scale = page.insert_htmlbox(
        rect,
        rich_html,
        css=css,
        archive=archive,
        scale_low=1,
        overlay=overlay,
    )
    return float(spare_height)


def _rect_area(rect: pymupdf.Rect) -> float:
    return max(0.0, rect.width) * max(0.0, rect.height)


def _overlap_area(left: pymupdf.Rect, right: pymupdf.Rect) -> float:
    width = min(left.x1, right.x1) - max(left.x0, right.x0)
    height = min(left.y1, right.y1) - max(left.y0, right.y0)
    return max(0.0, width) * max(0.0, height)


def _inflate_rect(rect: pymupdf.Rect, padding: float) -> pymupdf.Rect:
    return pymupdf.Rect(
        rect.x0 - padding,
        rect.y0 - padding,
        rect.x1 + padding,
        rect.y1 + padding,
    )


def _line_obstacle(start, end, padding: float) -> pymupdf.Rect:
    return pymupdf.Rect(
        min(start.x, end.x) - padding,
        min(start.y, end.y) - padding,
        max(start.x, end.x) + padding,
        max(start.y, end.y) + padding,
    )


def _page_art_obstacles(page) -> list[pymupdf.Rect]:
    """Return image bounds and stroked vector lines, while ignoring soft fills."""
    obstacles = [
        pymupdf.Rect(block["bbox"])
        for block in page.get_text("dict").get("blocks", [])
        if block.get("type") == 1 and block.get("bbox")
    ]
    for drawing in page.get_drawings():
        if drawing.get("color") is None:
            continue
        padding = max(1.0, float(drawing.get("width") or 1.0) / 2 + 0.75)
        for item in drawing.get("items", []):
            if item[0] == "l":
                obstacles.append(_line_obstacle(item[1], item[2], padding))
            elif item[0] == "re" and drawing.get("fill") is None:
                rect = pymupdf.Rect(item[1])
                obstacles.extend(
                    [
                        pymupdf.Rect(rect.x0 - padding, rect.y0 - padding, rect.x1 + padding, rect.y0 + padding),
                        pymupdf.Rect(rect.x0 - padding, rect.y1 - padding, rect.x1 + padding, rect.y1 + padding),
                        pymupdf.Rect(rect.x0 - padding, rect.y0, rect.x0 + padding, rect.y1),
                        pymupdf.Rect(rect.x1 - padding, rect.y0, rect.x1 + padding, rect.y1),
                    ]
                )
    return obstacles


def _layout_obstacles(
    segment: Segment,
    page_segments: list[Segment],
    art_obstacles: list[pymupdf.Rect],
    reserved_rects: list[pymupdf.Rect],
) -> list[pymupdf.Rect]:
    clearance = max(1.5, min(3.0, (segment.font_size or 11.0) * 0.2))
    obstacles = [
        _inflate_rect(pymupdf.Rect(json.loads(other.bbox_json)), clearance)
        for other in page_segments
        if other.id != segment.id
    ]
    obstacles.extend(_inflate_rect(rect, clearance) for rect in art_obstacles)
    obstacles.extend(_inflate_rect(rect, clearance) for rect in reserved_rects)
    return obstacles


def _candidate_is_clear(
    candidate: pymupdf.Rect,
    base: pymupdf.Rect,
    obstacles: list[pymupdf.Rect],
) -> bool:
    return all(
        _overlap_area(candidate, obstacle) <= _overlap_area(base, obstacle) + 0.25
        for obstacle in obstacles
    )


def _expansion_caps(
    base: pymupdf.Rect,
    page_rect: pymupdf.Rect,
    segment: Segment,
) -> tuple[float, float, float, float]:
    horizontal_cap = min(96.0, page_rect.width * 0.22)
    vertical_cap = min(72.0, page_rect.height * 0.12)
    if segment.kind == "table_cell":
        horizontal_cap = min(horizontal_cap, 48.0)
        vertical_cap = min(vertical_cap, 32.0)
    left = min(horizontal_cap, base.x0 - page_rect.x0 - 2.0)
    up = min(vertical_cap, base.y0 - page_rect.y0 - 2.0)
    right = min(horizontal_cap, page_rect.x1 - base.x1 - 2.0)
    down = min(vertical_cap, page_rect.y1 - base.y1 - 2.0)
    return tuple(max(0.0, value) for value in (left, up, right, down))


def _prune_boundaries(values: set[float], maximum: int = 32) -> list[float]:
    ordered = sorted(round(max(0.0, value), 3) for value in values)
    if len(ordered) <= maximum:
        return ordered
    indexes = {
        round(index * (len(ordered) - 1) / (maximum - 1))
        for index in range(maximum)
    }
    return [ordered[index] for index in sorted(indexes)]


def _horizontal_boundaries(
    base: pymupdf.Rect,
    obstacles: list[pymupdf.Rect],
    left_cap: float,
    right_cap: float,
) -> tuple[list[float], list[float]]:
    left_values = {0.0, left_cap}
    right_values = {0.0, right_cap}
    for obstacle in obstacles:
        if obstacle.x1 <= base.x0:
            distance = base.x0 - obstacle.x1
            if distance <= left_cap:
                left_values.add(distance)
        elif obstacle.x0 >= base.x1:
            distance = obstacle.x0 - base.x1
            if distance <= right_cap:
                right_values.add(distance)
    return _prune_boundaries(left_values), _prune_boundaries(right_values)


def _vertical_capacity(
    horizontal_rect: pymupdf.Rect,
    base: pymupdf.Rect,
    obstacles: list[pymupdf.Rect],
    up_cap: float,
    down_cap: float,
) -> tuple[float, float]:
    up = up_cap
    down = down_cap
    for obstacle in obstacles:
        horizontal_overlap = (
            min(horizontal_rect.x1, obstacle.x1)
            - max(horizontal_rect.x0, obstacle.x0)
        )
        if horizontal_overlap <= 0:
            continue
        if obstacle.y1 <= base.y0:
            up = min(up, base.y0 - obstacle.y1)
        elif obstacle.y0 >= base.y1:
            down = min(down, obstacle.y0 - base.y1)
    return max(0.0, up), max(0.0, down)


def _expanded_rect(
    base: pymupdf.Rect,
    values: tuple[float, float, float, float],
) -> pymupdf.Rect:
    left, up, right, down = values
    return pymupdf.Rect(base.x0 - left, base.y0 - up, base.x1 + right, base.y1 + down)


def _expansion_score(
    base: pymupdf.Rect,
    values: tuple[float, float, float, float],
) -> float:
    candidate = _expanded_rect(base, values)
    growth = (_rect_area(candidate) - _rect_area(base)) / max(1.0, _rect_area(base))
    left, up, right, down = values
    anchor_shift = left / max(1.0, base.width) + up / max(1.0, base.height)
    changed_edges = sum(value > 0 for value in values)
    # Preserve the original top-left anchor when similarly sized candidates both fit.
    return growth + anchor_shift * 0.35 + changed_edges * 0.015 + (right + down) * 0.0001


def _polygon_points(segment: Segment, sx: float, sy: float) -> list[tuple[int, int]]:
    if not segment.polygon_json:
        return []
    try:
        raw = json.loads(segment.polygon_json)
        return [
            (round(float(raw[index]) * sx), round(float(raw[index + 1]) * sy))
            for index in range(0, len(raw) - 1, 2)
        ]
    except (TypeError, ValueError, json.JSONDecodeError):
        return []


def _interpolate_smooth_mask(crop, mask, fallback):
    """Rebuild smooth backgrounds row-by-row without neural inpainting artifacts."""
    import numpy as np

    repaired = fallback.copy()
    for row_index in range(mask.shape[0]):
        masked = np.flatnonzero(mask[row_index] > 0)
        if masked.size == 0:
            continue
        start, end = int(masked[0]), int(masked[-1])
        if start == 0 or end >= mask.shape[1] - 1:
            continue
        left = crop[row_index, start - 1].astype(np.float32)
        right = crop[row_index, end + 1].astype(np.float32)
        weights = np.linspace(0, 1, end - start + 3, dtype=np.float32)[1:-1, None]
        repaired[row_index, start : end + 1] = np.clip(
            left * (1 - weights) + right * weights, 0, 255
        ).astype(np.uint8)
    return repaired


def _scanned_background_repair(
    page_row: Page, segment: Segment, rect: pymupdf.Rect
) -> BackgroundRepair:
    """Use a solid sampled fill for calm backgrounds and inpaint complex ones."""
    if not page_row.preview_path:
        return BackgroundRepair(fill=(1, 1, 1))
    try:
        import cv2
        import numpy as np

        with Image.open(page_row.preview_path).convert("RGB") as image:
            pixels = np.asarray(image)
            sx, sy = image.width / page_row.width, image.height / page_row.height
            inner_x0 = max(0, int(rect.x0 * sx))
            inner_y0 = max(0, int(rect.y0 * sy))
            inner_x1 = min(image.width, max(inner_x0 + 1, int(rect.x1 * sx + 0.999)))
            inner_y1 = min(image.height, max(inner_y0 + 1, int(rect.y1 * sy + 0.999)))
            # OCR polygons often stop at the visible glyph edge. A generous dilation removes
            # anti-aliased fringes and small polygon misses before inpainting.
            dilation = max(6, round((segment.font_size or 10.0) * min(sx, sy) * 0.65))
            margin = max(
                dilation + 4,
                round(min(inner_x1 - inner_x0, inner_y1 - inner_y0) * 0.20),
            )
            outer_x0 = max(0, inner_x0 - margin)
            outer_y0 = max(0, inner_y0 - margin)
            outer_x1 = min(image.width, inner_x1 + margin)
            outer_y1 = min(image.height, inner_y1 + margin)
            crop = pixels[outer_y0:outer_y1, outer_x0:outer_x1].copy()
            if crop.size == 0:
                return BackgroundRepair(fill=(1, 1, 1))

            mask = np.zeros(crop.shape[:2], dtype=np.uint8)
            points = _polygon_points(segment, 1.0, 1.0)
            if points:
                local_points = np.array(
                    [[x - outer_x0, y - outer_y0] for x, y in points], dtype=np.int32
                )
                cv2.fillPoly(mask, [local_points], 255)
            else:
                mask[
                    inner_y0 - outer_y0 : inner_y1 - outer_y0,
                    inner_x0 - outer_x0 : inner_x1 - outer_x0,
                ] = 255
            kernel_size = dilation * 2 + 1
            mask = cv2.dilate(
                mask, np.ones((kernel_size, kernel_size), dtype=np.uint8), iterations=1
            )

            ring_pixels = crop[mask == 0]
            if len(ring_pixels) < 16:
                median = np.median(crop.reshape(-1, 3), axis=0)
                spread = 0.0
            else:
                median = np.median(ring_pixels, axis=0)
                distances = np.linalg.norm(ring_pixels.astype(np.float32) - median, axis=1)
                spread = float(np.percentile(distances, 80))
            color = tuple(float(channel) / 255 for channel in median)
            if spread <= 18.0:
                return BackgroundRepair(fill=color)

            repaired_bgr = cv2.inpaint(
                cv2.cvtColor(crop, cv2.COLOR_RGB2BGR), mask, 3, cv2.INPAINT_TELEA
            )
            repaired_rgb = cv2.cvtColor(repaired_bgr, cv2.COLOR_BGR2RGB)
            gray = cv2.cvtColor(crop, cv2.COLOR_RGB2GRAY)
            laplacian = np.abs(cv2.Laplacian(gray, cv2.CV_32F))
            visible_edges = laplacian[mask == 0]
            edge_score = float(np.percentile(visible_edges, 90)) if len(visible_edges) else 0.0
            if edge_score <= 6.0:
                repaired_rgb = _interpolate_smooth_mask(crop, mask, repaired_rgb)
            output = io.BytesIO()
            Image.fromarray(repaired_rgb).save(output, format="PNG")
            patch_rect = pymupdf.Rect(
                outer_x0 / sx,
                outer_y0 / sy,
                outer_x1 / sx,
                outer_y1 / sy,
            )
            return BackgroundRepair(fill=False, patch_rect=patch_rect, patch_png=output.getvalue())
    except (ImportError, OSError, ValueError):
        return BackgroundRepair(fill=(1, 1, 1))


def _background_repair(
    page_row: Page, segment: Segment, rect: pymupdf.Rect
) -> BackgroundRepair:
    if page_row.page_type == "native":
        return BackgroundRepair(fill=False)
    return _scanned_background_repair(page_row, segment, rect)


def _minimum_font_size(segment: Segment) -> float:
    original_size = segment.font_size or 11.0
    return max(4.0, min(7.0, original_size), original_size * 0.65)


def _required_text_height(
    page,
    width: float,
    maximum_height: float,
    segment: Segment,
    font_name: str,
    font_file: Path | None,
    font_size: float,
) -> float | None:
    if width <= 1.0:
        return None
    rich_result = _insert_rich_textbox(
        page,
        pymupdf.Rect(1, 1, width + 1, maximum_height + 1),
        segment,
        font_file,
        font_size,
        overlay=False,
    )
    if rich_result is not None:
        return None if rich_result < 0 else maximum_height - rich_result
    shape = page.new_shape()
    result = shape.insert_textbox(
        pymupdf.Rect(1, 1, width + 1, maximum_height + 1),
        _layout_text(segment),
        fontname=font_name,
        fontfile=str(font_file) if font_file else None,
        fontsize=font_size,
        lineheight=0.92,
        color=_color_from_int(segment.font_color),
        align=pymupdf.TEXT_ALIGN_LEFT,
    )
    if result < 0:
        return None
    return maximum_height - result


def _fit_text_size(
    rect: pymupdf.Rect, segment: Segment, font_name: str, font_file: Path | None
) -> float | None:
    original_size = segment.font_size or 11.0
    minimum = _minimum_font_size(segment)
    size = original_size
    probe = pymupdf.open()
    try:
        page = probe.new_page(width=max(2.0, rect.width + 2), height=max(2.0, rect.height + 2))
        probe_rect = pymupdf.Rect(1, 1, rect.width + 1, rect.height + 1)
        while True:
            rich_result = _insert_rich_textbox(
                page,
                probe_rect,
                segment,
                font_file,
                size,
                overlay=False,
            )
            if rich_result is None:
                shape = page.new_shape()
                result = shape.insert_textbox(
                    probe_rect,
                    _layout_text(segment),
                    fontname=font_name,
                    fontfile=str(font_file) if font_file else None,
                    fontsize=size,
                    lineheight=0.92,
                    color=_color_from_int(segment.font_color),
                    align=pymupdf.TEXT_ALIGN_LEFT,
                )
            else:
                result = rich_result
            if result >= 0:
                return size
            if size <= minimum + 0.01:
                break
            size = max(minimum, size - 0.5)
        return None
    finally:
        probe.close()


def _find_expanded_rect(
    base: pymupdf.Rect,
    segment: Segment,
    page_rect: pymupdf.Rect,
    obstacles: list[pymupdf.Rect],
    font_name: str,
    font_file: Path | None,
) -> pymupdf.Rect | None:
    left_cap, up_cap, right_cap, down_cap = _expansion_caps(
        base, page_rect, segment
    )
    left_values, right_values = _horizontal_boundaries(
        base,
        obstacles,
        left_cap,
        right_cap,
    )
    minimum = _minimum_font_size(segment)
    text = _layout_text(segment)
    measurement_height = min(
        14_000.0,
        max(512.0, page_rect.height * 4, len(text) * minimum * 1.5),
    )
    height_cache: dict[float, float | None] = {}
    best: tuple[float, pymupdf.Rect] | None = None
    probe = pymupdf.open()
    try:
        probe_page = probe.new_page(
            width=page_rect.width + 2,
            height=measurement_height + 2,
        )
        for left in left_values:
            for right in right_values:
                if left <= 0 and right <= 0:
                    continue
                horizontal = _expanded_rect(base, (left, 0.0, right, 0.0))
                if not _candidate_is_clear(horizontal, base, obstacles):
                    continue
                up, down = _vertical_capacity(
                    horizontal,
                    base,
                    obstacles,
                    up_cap,
                    down_cap,
                )
                width_key = round(horizontal.width, 3)
                if width_key not in height_cache:
                    height_cache[width_key] = _required_text_height(
                        probe_page,
                        horizontal.width,
                        measurement_height,
                        segment,
                        font_name,
                        font_file,
                        minimum,
                    )
                required_height = height_cache[width_key]
                if required_height is None:
                    continue
                extra_height = max(0.0, required_height + 0.25 - base.height)
                if extra_height > up + down + 0.01:
                    continue
                use_down = min(extra_height, down)
                use_up = max(0.0, extra_height - use_down)
                values = (left, use_up, right, use_down)
                candidate = _expanded_rect(base, values)
                if not _candidate_is_clear(candidate, base, obstacles):
                    continue
                score = _expansion_score(base, values)
                if best is None or score < best[0]:
                    best = (score, candidate)

        # Vertical-only expansion uses the original width, which was excluded above.
        up, down = _vertical_capacity(base, base, obstacles, up_cap, down_cap)
        width_key = round(base.width, 3)
        required_height = height_cache.get(width_key)
        if width_key not in height_cache:
            required_height = _required_text_height(
                probe_page,
                base.width,
                measurement_height,
                segment,
                font_name,
                font_file,
                minimum,
            )
            height_cache[width_key] = required_height
        if required_height is not None:
            extra_height = max(0.0, required_height + 0.25 - base.height)
            if extra_height <= up + down + 0.01:
                use_down = min(extra_height, down)
                use_up = max(0.0, extra_height - use_down)
                values = (0.0, use_up, 0.0, use_down)
                candidate = _expanded_rect(base, values)
                if _candidate_is_clear(candidate, base, obstacles):
                    score = _expansion_score(base, values)
                    if best is None or score < best[0]:
                        best = (score, candidate)
        return best[1] if best else None
    finally:
        probe.close()


def _fit_segment_layout(
    segment: Segment,
    page_segments: list[Segment],
    page_rect: pymupdf.Rect,
    page_type: str,
    art_obstacles: list[pymupdf.Rect],
    reserved_rects: list[pymupdf.Rect],
    font_name: str,
    font_file: Path | None,
) -> tuple[pymupdf.Rect, float] | None:
    base = pymupdf.Rect(json.loads(segment.bbox_json))
    fitted_size = _fit_text_size(base, segment, font_name, font_file)
    if fitted_size is not None:
        return base, fitted_size
    if page_type != "native":
        return None
    obstacles = _layout_obstacles(segment, page_segments, art_obstacles, reserved_rects)
    expanded = _find_expanded_rect(
        base,
        segment,
        page_rect,
        obstacles,
        font_name,
        font_file,
    )
    if expanded is None:
        return None
    fitted_size = _fit_text_size(expanded, segment, font_name, font_file)
    if fitted_size is None:
        return None
    return expanded, fitted_size


def _maximum_fitting_characters(
    segment: Segment,
    page_segments: list[Segment],
    page_rect: pymupdf.Rect,
    page_type: str,
    art_obstacles: list[pymupdf.Rect],
    reserved_rects: list[pymupdf.Rect],
    font_name: str,
    font_file: Path | None,
) -> int:
    original = segment.target_text
    text = _layout_text(segment)
    if not text:
        return 0
    low, high, best = 1, len(text), 0
    try:
        while low <= high:
            middle = (low + high) // 2
            segment.target_text = text[:middle]
            if _fit_segment_layout(
                segment,
                page_segments,
                page_rect,
                page_type,
                art_obstacles,
                reserved_rects,
                font_name,
                font_file,
            ) is not None:
                best = middle
                low = middle + 1
            else:
                high = middle - 1
        return best
    finally:
        segment.target_text = original


def find_compression_candidates(db: Session, job: Job) -> list[CompressionCandidate]:
    """Find translations that fail both safe layout strategies and estimate a hard text budget."""
    password = secret_store.get(f"pdf-password:{job.id}") or ""
    doc = pymupdf.open(job.source_path)
    if doc.needs_pass and doc.authenticate(password) <= 0:
        doc.close()
        raise FinalQualityGateError("无法打开源 PDF")
    font_file = _font_file(job)
    font_name = "foliofont" if font_file else "helv"
    pages = {
        row.page_number: row
        for row in db.scalars(select(Page).where(Page.job_id == job.id))
    }
    candidates: list[CompressionCandidate] = []
    try:
        for page_number, pdf_page in enumerate(doc, start=1):
            page_row = pages[page_number]
            segments = list(
                db.scalars(
                    select(Segment)
                    .where(Segment.page_id == page_row.id)
                    .order_by(Segment.reading_order)
                )
            )
            translated = [
                segment
                for segment in segments
                if segment.target_text and not segment.ignored and segment.status != "skipped"
            ]
            art_obstacles = (
                _page_art_obstacles(pdf_page) if page_row.page_type == "native" else []
            )
            reserved_rects: list[pymupdf.Rect] = []
            for segment in translated:
                layout = _fit_segment_layout(
                    segment,
                    segments,
                    pdf_page.rect,
                    page_row.page_type,
                    art_obstacles,
                    reserved_rects,
                    font_name,
                    font_file,
                )
                if layout is not None:
                    reserved_rects.append(layout[0])
                    continue
                maximum = _maximum_fitting_characters(
                    segment,
                    segments,
                    pdf_page.rect,
                    page_row.page_type,
                    art_obstacles,
                    reserved_rects,
                    font_name,
                    font_file,
                )
                if maximum < len(_layout_text(segment)):
                    candidates.append(
                        CompressionCandidate(segment=segment, max_characters=maximum)
                    )
        return candidates
    finally:
        doc.close()


def _insert_prepared_text(page, prepared: PreparedTranslation, font_name: str, font_file: Path | None) -> bool:
    rich_result = _insert_rich_textbox(
        page,
        prepared.rect,
        prepared.segment,
        font_file,
        prepared.font_size,
        overlay=True,
    )
    if rich_result is not None:
        return rich_result >= 0
    result = page.insert_textbox(
        prepared.rect,
        _layout_text(prepared.segment),
        fontname=font_name,
        fontfile=str(font_file) if font_file else None,
        fontsize=prepared.font_size,
        lineheight=0.92,
        color=_color_from_int(prepared.segment.font_color),
        align=pymupdf.TEXT_ALIGN_LEFT,
        overlay=True,
    )
    return result >= 0


def _render_translated(db: Session, job: Job, output: Path) -> None:
    password = secret_store.get(f"pdf-password:{job.id}") or ""
    doc = pymupdf.open(job.source_path)
    if doc.needs_pass and doc.authenticate(password) <= 0:
        raise FinalQualityGateError("无法打开源 PDF")
    # Do not propagate active content, embedded files or form state into translated output.
    doc.scrub(
        attached_files=True,
        embedded_files=True,
        javascript=True,
        reset_fields=True,
        remove_links=False,
        metadata=False,
        hidden_text=False,
    )
    font_file = _font_file(job)
    font_name = "foliofont" if font_file else "helv"
    db.execute(
        delete(QualityIssue).where(
            QualityIssue.job_id == job.id,
            QualityIssue.code == "overflow",
            QualityIssue.acknowledged.is_(False),
        )
    )
    pages = {row.page_number: row for row in db.scalars(select(Page).where(Page.job_id == job.id))}
    for page_number, pdf_page in enumerate(doc, start=1):
        page_row = pages[page_number]
        segments = list(
            db.scalars(
                select(Segment)
                .where(Segment.page_id == page_row.id)
                .order_by(Segment.reading_order)
            )
        )
        translated = [
            segment
            for segment in segments
            if segment.target_text and not segment.ignored and segment.status != "skipped"
        ]
        art_obstacles = _page_art_obstacles(pdf_page) if page_row.page_type == "native" else []
        reserved_rects: list[pymupdf.Rect] = []
        prepared_translations: list[PreparedTranslation] = []
        for segment in translated:
            layout = _fit_segment_layout(
                segment,
                segments,
                pdf_page.rect,
                page_row.page_type,
                art_obstacles,
                reserved_rects,
                font_name,
                font_file,
            )
            if layout is None:
                add_issue(
                    db,
                    job.id,
                    "overflow",
                    "译文在最小可读字号及四向安全扩框后仍无法放置；已保留原文",
                    severity="error",
                    segment_id=segment.id,
                )
                continue
            rect, fitted_size = layout
            repair = _background_repair(page_row, segment, rect)
            prepared_translations.append(
                PreparedTranslation(
                    segment=segment,
                    rect=rect,
                    font_size=fitted_size,
                    repair=repair,
                )
            )
            reserved_rects.append(rect)
            pdf_page.add_redact_annot(rect, fill=repair.fill, cross_out=False)
        if prepared_translations:
            try:
                pdf_page.apply_redactions(
                    images=pymupdf.PDF_REDACT_IMAGE_NONE,
                    graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                )
            except TypeError:
                pdf_page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE)
        for prepared in prepared_translations:
            repair = prepared.repair
            if repair.patch_rect is not None and repair.patch_png:
                pdf_page.insert_image(
                    repair.patch_rect,
                    stream=repair.patch_png,
                    keep_proportion=False,
                    overlay=True,
                )
        for prepared in prepared_translations:
            if not _insert_prepared_text(pdf_page, prepared, font_name, font_file):
                add_issue(
                    db,
                    job.id,
                    "overflow",
                    "译文通过排版预检，但实际写入失败",
                    severity="error",
                    segment_id=prepared.segment.id,
                )
    output.parent.mkdir(parents=True, exist_ok=True)
    doc.save(output, garbage=4, deflate=True)
    doc.close()
    db.commit()


def _render_bilingual(source_path: Path, translated_path: Path, output: Path) -> None:
    source = pymupdf.open(source_path)
    translated = pymupdf.open(translated_path)
    combined = pymupdf.open()
    gutter = 24.0
    for page_index in range(len(source)):
        source_page = source[page_index]
        translated_page = translated[page_index]
        width = source_page.rect.width + translated_page.rect.width + gutter
        height = max(source_page.rect.height, translated_page.rect.height)
        page = combined.new_page(width=width, height=height)
        page.show_pdf_page(pymupdf.Rect(0, 0, source_page.rect.width, source_page.rect.height), source, page_index)
        page.show_pdf_page(
            pymupdf.Rect(
                source_page.rect.width + gutter,
                0,
                source_page.rect.width + gutter + translated_page.rect.width,
                translated_page.rect.height,
            ),
            translated,
            page_index,
        )
        page.draw_line(
            pymupdf.Point(source_page.rect.width + gutter / 2, 16),
            pymupdf.Point(source_page.rect.width + gutter / 2, height - 16),
            color=(0.55, 0.50, 0.44),
            width=0.5,
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    combined.save(output, garbage=4, deflate=True)
    combined.close()
    translated.close()
    source.close()


def render_artifact(db: Session, job: Job, mode: str, final: bool) -> Artifact:
    if final and unresolved_count(db, job.id):
        raise FinalQualityGateError("仍有未处理或未确认的质量问题，不能生成无标识终稿")
    job_dir = Path(job.source_path).parent
    suffix = "final" if final else "draft"
    translated_path = job_dir / f"translated-{suffix}.pdf"
    _render_translated(db, job, translated_path)
    if final and unresolved_count(db, job.id):
        translated_path.unlink(missing_ok=True)
        raise FinalQualityGateError("渲染发现文字溢出，请处理后再生成终稿")
    if mode == "translated":
        output = translated_path
    elif mode == "bilingual":
        output = job_dir / f"bilingual-{suffix}.pdf"
        _render_bilingual(Path(job.source_path), translated_path, output)
    else:
        raise ValueError("未知输出模式")
    data = output.read_bytes()
    kind = f"{mode}_{suffix}"
    artifact = db.scalar(select(Artifact).where(Artifact.job_id == job.id, Artifact.kind == kind))
    if artifact is None:
        artifact = Artifact(job_id=job.id, kind=kind, path=str(output))
        db.add(artifact)
    artifact.path = str(output)
    artifact.size_bytes = len(data)
    artifact.sha256 = hashlib.sha256(data).hexdigest()
    db.commit()
    return artifact
