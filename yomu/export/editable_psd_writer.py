"""Editable Professional PSD (v2) writer -- native Photoshop Type Layers.

Emits one PSD per logical page with three groups:

    ORIGINAL  (hidden)   ORIGINAL_ART   -- source page, raster
    CLEAN     (visible)  CLEAN_ARTWORK  -- canonical clean art (Mission 4), raster, default base
    TEXT      (visible)  one NATIVE Type Layer per text region (BALAO_001, NARRACAO_001, ...)

The translation lives in real Photoshop Type (Text) layers -- never rasterized -- so a QC
editor opens the PSD and edits text with the Type tool. The Type-Layer mechanism (a TySh
``TypeToolObjectSetting`` block carrying a hand-built Adobe EngineData document) is the exact
one physically verified in Adobe Photoshop during the Mission 5 spike.

Editability is prioritized over cosmetic fidelity (Mission 6 principle): approximate style is
acceptable, rasterizing text to chase an effect is not. Advanced effects (outline/stroke,
rotation, bold/italic synthesis) are intentionally out of scope and recorded as limitations.

Depends only on psd-tools (already a project dependency) and Pillow.
"""
from __future__ import annotations

import codecs
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image, ImageCms, ImageDraw, ImageFont

from psd_tools import PSDImage
from psd_tools.api.layers import Group, PixelLayer
from psd_tools.constants import Resource, Tag
from psd_tools.psd.engine_data import (
    Bool as EBool, Dict as EDict, EngineData, Float as EFloat,
    Integer as EInt, List as EList, String as EStr,
)
from psd_tools.psd.descriptor import (
    Descriptor, Double, Enumerated, Integer as DInt, RawData, String as DStr,
    Unit, UnitFloat,
)
from psd_tools.psd.image_resources import ImageResource
from psd_tools.psd.tagged_blocks import DescriptorBlock, TaggedBlock, TypeToolObjectSetting

DEFAULT_FONT_FAMILY = "ArialMT"
PRODUCTION_TEXT_MODE = "POINT"  # the mode physically confirmed editable in Photoshop (M5)

# Deterministic layer-name prefixes by semantic classification.
_TYPE_PREFIX = {
    "speech": "BALAO", "speech_balloon": "BALAO", "balloon": "BALAO", "dialogue": "BALAO",
    "narration": "NARRACAO", "narrative": "NARRACAO",
    "caption": "CAPTION",
    "footnote": "FOOTNOTE", "note": "FOOTNOTE",
    "title": "TITLE", "heading": "TITLE",
    "sfx": "SFX", "sound_effect": "SFX",
}
_DEFAULT_PREFIX = "TEXT"


class EditablePsdError(RuntimeError):
    pass


@dataclass
class EditableTextRegion:
    """Decoupled input for one text region (Mission 6 §42). The writer never touches the
    pipeline's own giant objects; the caller fills these explicit fields."""
    region_id: str
    semantic_type: str
    text: str                       # the content to place (translation, or source on fallback)
    bbox: tuple                     # (x, y, w, h) in canvas pixels
    source_text: str = ""
    translation_status: str = "translated"  # translated | source_fallback | review_required
    font_family: str = DEFAULT_FONT_FAMILY
    font_size: int = 24
    color: tuple = (0, 0, 0)        # RGB 0..255
    alignment: str = "left"        # left | center | right
    bold: bool = False
    italic: bool = False
    rotation: float = 0.0
    review_required: bool = False
    # provenance: which fields came from the renderer vs a fallback (Mission 7 §19)
    sources: dict = field(default_factory=dict)


# ======================================================================================
# EngineData (Adobe text-engine document) -- ported verbatim from the M5 spike.
# ======================================================================================
def _skey(k):
    return k.decode("latin-1") if isinstance(k, bytes) else k


def _D(pairs):
    d = EDict()
    for k, v in pairs:
        d[_skey(k)] = v
    return d


def _color_engine(values):
    return _D([(b"Type", EInt(1)), (b"Values", EList([EFloat(v) for v in values]))])


def _style_sheet_data(font_index, font_size, rgb, bold, italic):
    return _D([
        (b"Font", EInt(font_index)),
        (b"FontSize", EFloat(float(font_size))),
        (b"FauxBold", EBool(bool(bold))),
        (b"FauxItalic", EBool(bool(italic))),
        (b"AutoLeading", EBool(True)),
        (b"Leading", EFloat(float(font_size) * 1.2)),
        (b"HorizontalScale", EFloat(1.0)),
        (b"VerticalScale", EFloat(1.0)),
        (b"Tracking", EInt(0)),
        (b"AutoKerning", EBool(True)),
        (b"Kerning", EInt(0)),
        (b"BaselineShift", EFloat(0.0)),
        (b"FontCaps", EInt(0)),
        (b"FontBaseline", EInt(0)),
        (b"Underline", EBool(False)),
        (b"Strikethrough", EBool(False)),
        (b"Ligatures", EBool(True)),
        (b"DLigatures", EBool(False)),
        (b"BaselineDirection", EInt(2)),
        (b"StyleRunAlignment", EInt(2)),
        (b"Language", EInt(0)),
        (b"NoBreak", EBool(False)),
        (b"FillColor", _color_engine([1.0, rgb[0] / 255.0, rgb[1] / 255.0, rgb[2] / 255.0])),
        (b"StrokeColor", _color_engine([1.0, 0.0, 0.0, 0.0])),
        (b"FillFlag", EBool(True)),
        (b"StrokeFlag", EBool(False)),
        (b"FillFirst", EBool(True)),
    ])


_ALIGN = {"left": 0, "right": 1, "center": 2, "justify": 3}


def _paragraph_sheet(justification):
    return _D([
        (b"Justification", EInt(justification)),
        (b"FirstLineIndent", EFloat(0.0)),
        (b"StartIndent", EFloat(0.0)),
        (b"EndIndent", EFloat(0.0)),
        (b"SpaceBefore", EFloat(0.0)),
        (b"SpaceAfter", EFloat(0.0)),
        (b"AutoHyphenate", EBool(True)),
        (b"HyphenatedWordSize", EInt(6)),
        (b"PreHyphen", EInt(2)),
        (b"PostHyphen", EInt(2)),
        (b"ConsecutiveHyphens", EInt(8)),
        (b"Zone", EFloat(36.0)),
        (b"WordSpacing", EList([EFloat(0.8), EFloat(1.0), EFloat(1.33)])),
        (b"LetterSpacing", EList([EFloat(0.0), EFloat(0.0), EFloat(0.0)])),
        (b"GlyphSpacing", EList([EFloat(1.0), EFloat(1.0), EFloat(1.0)])),
        (b"AutoLeading", EFloat(1.2)),
        (b"LeadingType", EInt(0)),
        (b"Hanging", EBool(False)),
        (b"Burasagari", EBool(False)),
        (b"KinsokuOrder", EInt(0)),
        (b"EveryLineComposer", EBool(False)),
    ])


def _rendered_shapes():
    child = _D([
        (b"ShapeType", EInt(0)),
        (b"Procession", EInt(0)),
        (b"Lines", _D([(b"WritingDirection", EInt(0)), (b"Children", EList([]))])),
        (b"Cookie", _D([(b"Photoshop", _D([
            (b"ShapeType", EInt(0)),
            (b"PointBase", EList([EFloat(0.0), EFloat(0.0)])),
            (b"Base", _D([
                (b"ShapeType", EInt(0)),
                (b"TransformPoint0", EList([EFloat(1.0), EFloat(0.0)])),
                (b"TransformPoint1", EList([EFloat(0.0), EFloat(1.0)])),
                (b"TransformPoint2", EList([EFloat(0.0), EFloat(0.0)])),
            ])),
        ]))])),
    ])
    return _D([
        (b"Version", EInt(1)),
        (b"Shapes", _D([(b"WritingDirection", EInt(0)), (b"Children", EList([child]))])),
    ])


def _build_engine_data(text, font_family, font_size, rgb, justification, bold, italic):
    ps_text = text.replace("\n", "\r")
    n = len(ps_text)
    style_run = _D([
        (b"DefaultRunData", _D([(b"StyleSheet", _D([(b"StyleSheetData", _D([]))]))])),
        (b"RunArray", EList([_D([(b"StyleSheet", _D([
            (b"StyleSheetData", _style_sheet_data(0, font_size, rgb, bold, italic))]))])])),
        (b"RunLengthArray", EList([EInt(n)])),
        (b"IsJoinable", EInt(1)),
    ])
    paragraph_run = _D([
        (b"DefaultRunData", _D([
            (b"ParagraphSheet", _D([(b"DefaultStyleSheet", EInt(0)), (b"Properties", _D([]))])),
            (b"Adjustments", _D([(b"Axis", EList([EFloat(1.0), EFloat(0.0), EFloat(0.0)])),
                                 (b"XY", EList([EFloat(0.0), EFloat(0.0)]))])),
        ])),
        (b"RunArray", EList([_D([
            (b"ParagraphSheet", _D([(b"DefaultStyleSheet", EInt(0)),
                                    (b"Properties", _paragraph_sheet(justification))])),
            (b"Adjustments", _D([(b"Axis", EList([EFloat(1.0), EFloat(0.0), EFloat(0.0)])),
                                 (b"XY", EList([EFloat(0.0), EFloat(0.0)]))])),
        ])])),
        (b"RunLengthArray", EList([EInt(n)])),
    ])
    engine_dict = _D([
        (b"Editor", _D([(b"Text", EStr(ps_text))])),
        (b"ParagraphRun", paragraph_run),
        (b"StyleRun", style_run),
        (b"GridInfo", _D([
            (b"GridIsOn", EBool(False)), (b"ShowGrid", EBool(False)),
            (b"GridSize", EFloat(18.0)), (b"GridLeading", EFloat(22.0)),
            (b"GridColor", _color_engine([1.0, 0.0, 0.0, 1.0])),
            (b"GridLeadingFillColor", _color_engine([1.0, 0.0, 0.0, 1.0])),
            (b"AlignLineHeightToGridFlags", EBool(False)),
        ])),
        (b"AntiAlias", EInt(4)),
        (b"UseFractionalGlyphWidths", EBool(True)),
        (b"Rendered", _rendered_shapes()),
    ])
    doc_resources = _D([
        (b"KinsokuSet", EList([])), (b"MojiKumiSet", EList([])),
        (b"TheNormalStyleSheet", EInt(0)), (b"TheNormalParagraphSheet", EInt(0)),
        (b"ParagraphSheetSet", EList([_D([
            (b"Name", EStr("Normal RGB")), (b"DefaultStyleSheet", EInt(0)),
            (b"Properties", _paragraph_sheet(justification))])])),
        (b"StyleSheetSet", EList([_D([
            (b"Name", EStr("Normal RGB")),
            (b"StyleSheetData", _style_sheet_data(0, font_size, rgb, bold, italic))])])),
        (b"FontSet", EList([_D([
            (b"Name", EStr(font_family)), (b"Script", EInt(0)),
            (b"FontType", EInt(1)), (b"Synthetic", EInt(0))])])),
        (b"SuperscriptSize", EFloat(0.583)), (b"SuperscriptPosition", EFloat(0.333)),
        (b"SubscriptSize", EFloat(0.583)), (b"SubscriptPosition", EFloat(0.333)),
        (b"SmallCapSize", EFloat(0.7)),
    ])
    ed = EngineData()
    ed["DocumentResources"] = doc_resources
    ed["EngineDict"] = engine_dict
    ed["ResourceDict"] = doc_resources
    return ed


def _unit_point(v):
    return UnitFloat(unit=Unit.Points, value=float(v))


def _build_tysh(region: EditableTextRegion):
    x, y, w, h = [int(v) for v in region.bbox]
    justification = _ALIGN.get(region.alignment, 0)
    engine = _build_engine_data(region.text, region.font_family, region.font_size,
                                region.color, justification, region.bold, region.italic)
    ps_text = region.text.replace("\n", "\r")
    fs = region.font_size
    bounds_pts = (0.0, -fs, float(w), float(h - fs))
    bd = Descriptor(name="", classID=b"bounds", items=[
        (b"Left", _unit_point(bounds_pts[0])), (b"Top", _unit_point(bounds_pts[1])),
        (b"Rght", _unit_point(bounds_pts[2])), (b"Btom", _unit_point(bounds_pts[3]))])
    bbx = Descriptor(name="", classID=b"boundingBox", items=[
        (b"Left", _unit_point(bounds_pts[0])), (b"Top", _unit_point(bounds_pts[1])),
        (b"Rght", _unit_point(bounds_pts[2])), (b"Btom", _unit_point(bounds_pts[3]))])
    text_data = DescriptorBlock(name="", classID=b"TxLr", items=[
        (b"Txt ", DStr(ps_text)),
        (b"textGridding", Enumerated(typeID=b"textGridding", enum=b"None")),
        (b"Ornt", Enumerated(typeID=b"Ornt", enum=b"Hrzn")),
        (b"AntA", Enumerated(typeID=b"Annt", enum=b"antiAliasSharp")),
        (b"bounds", bd), (b"boundingBox", bbx),
        (b"TextIndex", DInt(0)), (b"EngineData", RawData(value=engine)),
    ])
    warp = DescriptorBlock(name="", classID=b"warp", items=[
        (b"warpStyle", Enumerated(typeID=b"warpStyle", enum=b"warpNone")),
        (b"warpValue", Double(0.0)), (b"warpPerspective", Double(0.0)),
        (b"warpPerspectiveOther", Double(0.0)),
        (b"warpRotate", Enumerated(typeID=b"Ornt", enum=b"Hrzn"))])
    # anchor at the region's top-left; baseline offset by font size (point text).
    ty = float(y + fs)
    return TypeToolObjectSetting(
        version=1, transform=(1.0, 0.0, 0.0, 1.0, float(x), ty),
        text_version=50, text_data=text_data, warp_version=1, warp=warp,
        left=int(x), top=int(y), right=int(x + w), bottom=int(y + h))


# ======================================================================================
# Raster appearance + assembly
# ======================================================================================
def _font_for(size, family_path=None):
    candidates = []
    if family_path:
        candidates.append(family_path)
    candidates += ["arial.ttf", "Arial.ttf", "DejaVuSans.ttf"]
    for name in candidates:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _text_appearance(canvas_size, region: EditableTextRegion, family_path=None):
    img = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    x, y, _w, _h = [int(v) for v in region.bbox]
    anchor = "ma" if region.alignment == "center" else "la"
    ax = x + (int(region.bbox[2]) // 2 if region.alignment == "center" else 0)
    d.multiline_text((ax, y), region.text.replace("\r", "\n"),
                     font=_font_for(region.font_size, family_path),
                     fill=(region.color[0], region.color[1], region.color[2], 255),
                     spacing=6, align=region.alignment, anchor=anchor)
    return img


def _bgr_or_path_to_rgba(src):
    if isinstance(src, (str, Path)):
        return Image.open(src).convert("RGBA")
    if isinstance(src, np.ndarray):
        arr = src[:, :, ::-1] if src.ndim == 3 and src.shape[2] == 3 else src
        return Image.fromarray(arr).convert("RGBA")
    if isinstance(src, Image.Image):
        return src.convert("RGBA")
    raise EditablePsdError("unsupported_image_source")


def _srgb_icc_bytes():
    try:
        return ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    except Exception:  # noqa: BLE001
        return b""


def assign_layer_names(regions):
    """Deterministic, human-friendly names per semantic type; a review/fallback region gets a
    ``_REVIEW`` suffix so QC sees it needs attention (§14)."""
    counters = {}
    names = []
    for r in regions:
        prefix = _TYPE_PREFIX.get((r.semantic_type or "").strip().lower(), _DEFAULT_PREFIX)
        counters[prefix] = counters.get(prefix, 0) + 1
        name = f"{prefix}_{counters[prefix]:03d}"
        if r.translation_status in ("source_fallback", "review_required") or r.review_required:
            name += "_REVIEW"
        names.append(name)
    return names


def write_editable_psd(output_path, original_image, clean_image, text_regions,
                       canvas_size, metadata=None, *, font_path=None,
                       original_visible=False, embed_srgb=True):
    """Write one editable PSD atomically. Returns a dict of what was written (for the manifest).

    ``original_image`` / ``clean_image`` may be a path, a BGR ndarray, or a PIL image.
    ``text_regions`` is a list of :class:`EditableTextRegion`.
    """
    metadata = metadata or {}
    W, H = canvas_size
    original_pil = _bgr_or_path_to_rgba(original_image)
    clean_pil = _bgr_or_path_to_rgba(clean_image)
    if original_pil.size != (W, H):
        original_pil = original_pil.resize((W, H))
    if clean_pil.size != (W, H):
        clean_pil = clean_pil.resize((W, H))

    psd = PSDImage.new("RGBA", (W, H))

    icc_mode = "NONE"
    if embed_srgb:
        icc = _srgb_icc_bytes()
        if icc:
            psd.image_resources[Resource.ICC_PROFILE] = ImageResource(
                signature=b"8BIM", key=Resource.ICC_PROFILE, name="", data=icc)
            icc_mode = "SRGB"

    g_original = Group.new("ORIGINAL", parent=psd)
    PixelLayer.frompil(original_pil, psd, layer_name="ORIGINAL_ART").move_to_group(g_original)
    g_original.visible = bool(original_visible)

    g_clean = Group.new("CLEAN", parent=psd)
    PixelLayer.frompil(clean_pil, psd, layer_name="CLEAN_ARTWORK").move_to_group(g_clean)
    g_clean.visible = True

    g_text = Group.new("TEXT", parent=psd)
    g_text.visible = True
    names = assign_layer_names(text_regions)
    written_layers = []
    for region, name in zip(text_regions, names):
        appearance = _text_appearance((W, H), region, font_path)
        layer = PixelLayer.frompil(appearance, psd, layer_name=name)
        layer._record.tagged_blocks[Tag.TYPE_TOOL_OBJECT_SETTING] = TaggedBlock(
            key=Tag.TYPE_TOOL_OBJECT_SETTING, data=_build_tysh(region))
        layer.move_to_group(g_text)
        written_layers.append({"name": name, "region_id": region.region_id,
                               "semantic_type": region.semantic_type,
                               "translation_status": region.translation_status,
                               "text": region.text, "bbox": list(region.bbox),
                               "font_family": region.font_family, "font_size": region.font_size,
                               "color": list(region.color), "alignment": region.alignment,
                               "bold": region.bold, "italic": region.italic,
                               "typography_sources": dict(region.sources)})

    dest = Path(output_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{dest.name}.{os.getpid()}.tmp")
    try:
        psd.save(tmp)
        _validate_structure(tmp, W, H, names)
        os.replace(tmp, dest)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
    return {"path": str(dest), "canvas": [W, H], "color_profile": icc_mode,
            "text_layers": written_layers, "text_mode": PRODUCTION_TEXT_MODE}


def _validate_structure(path, width, height, expected_text_names):
    """Structural validation without Photoshop (§33): reopen and confirm groups, rasters,
    native Type Layers and recoverable text. Raises EditablePsdError on any mismatch."""
    psd = PSDImage.open(path)
    if psd.width != width or psd.height != height:
        raise EditablePsdError(f"psd_dim_mismatch {psd.width}x{psd.height} != {width}x{height}")
    groups = {l.name: l for l in psd if l.is_group()}
    for gname in ("ORIGINAL", "CLEAN", "TEXT"):
        if gname not in groups:
            raise EditablePsdError(f"missing_group_{gname}")
    if not any(l.kind == "pixel" for l in groups["ORIGINAL"]):
        raise EditablePsdError("original_raster_missing")
    if not any(l.kind == "pixel" for l in groups["CLEAN"]):
        raise EditablePsdError("clean_raster_missing")
    type_layers = [l for l in groups["TEXT"] if l.kind == "type"]
    if len(type_layers) != len(expected_text_names):
        raise EditablePsdError(
            f"type_layer_count {len(type_layers)} != {len(expected_text_names)}")
    got = {l.name for l in type_layers}
    if got != set(expected_text_names):
        raise EditablePsdError(f"type_layer_names {got} != {set(expected_text_names)}")
    for l in type_layers:
        try:
            _ = l.text
        except Exception as exc:  # noqa: BLE001
            raise EditablePsdError(f"type_layer_text_unreadable_{l.name}") from exc
    return True
