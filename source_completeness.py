"""Contract: source content attributed to a group may not disappear silently.

TDD #32 made the loss *observable* (immutable raw OCR snapshots, stable line ids,
group membership and render-input snapshots).  This module makes it *detectable*:
it derives, for one group, the lexical evidence its own provenance says the group
owns, and checks that the downstream representation (group text, render input,
render geometry) still accounts for it.

Two properties matter and are deliberately narrow:

* Legitimate transformation is not loss.  Spacing repair, case folding,
  punctuation normalization, line merges and splits all preserve the lexical
  content, so the comparison runs on accent-folded, space-free letter sequences
  rather than on raw strings.  ``LOSTCONTROL`` and ``LOST CONTROL`` are the same
  evidence.
* Loss the provenance cannot explain is never ``pass``.  A token that vanished
  without a discard event carrying a reason the pipeline actually produces is an
  unexplained loss, and unexplained loss fails closed.

Legacy runs predate the provenance and report ``unavailable``.  Nothing here
reconstructs source truth from the final groups: that is exactly the mutated
representation the contract exists to distrust.
"""

from __future__ import annotations

import re
import unicodedata

STATUS_PASS = "pass"
STATUS_REVIEW = "review"
STATUS_FAIL = "fail"
STATUS_UNAVAILABLE = "unavailable"

EXPECTED_SOURCE_BASIS = "ocr_line_provenance"

# Shorter fragments are dominated by OCR noise and by the articles/particles that
# normalization legitimately rewrites; the physical validator already uses the
# same floor for the tokens it is willing to call residual source text.
MIN_MEANINGFUL_CHARS = 3

# A downstream box may be padded or trimmed by a few pixels by mask/safe-area
# normalization without excluding any source glyph.  Beyond that the source line
# is genuinely outside the region the renderer was given.
GEOMETRY_TOLERANCE_PX = 8

# Discard reasons the pipeline actually emits.  Deliberately an allowlist of real
# pipeline semantics: a reason invented to silence this check would defeat it.
KNOWN_DISCARD_REASONS = frozenset(
    {
        # _line_ignore_reason
        "empty_text",
        "too_few_useful_chars",
        "number_or_symbols_only",
        "low_confidence",
        "box_too_small",
        "box_too_large",
        "low_alpha_ratio",
        "noise_like_text",
        # _filter_groups / _classify_groups
        "empty_group",
        "low_group_confidence",
        "group_too_small",
        "group_too_large",
        "noise_like_group",
        "sfx_translation_disabled",
        "decorative_text",
        "weak_unknown_text",
    }
)

# ``record_replacement`` attributes drops to the pass that superseded them.
KNOWN_DISCARD_PREFIXES = ("replaced_by_",)


def fold_tokens(text):
    """Accent-folded upper-case word tokens, in reading order."""

    normalized = unicodedata.normalize("NFKD", str(text or ""))
    folded = "".join(char for char in normalized if not unicodedata.combining(char))
    return re.findall(r"[A-Z']+", folded.upper())


def compact(text):
    """Accent-folded letters with every separator removed.

    Whitespace is what OCR spacing repair adds and removes, so dropping it is
    what lets ``LOSTCONTROL`` and ``LOST CONTROL`` compare equal.
    """

    return "".join(fold_tokens(text))


def meaningful_tokens(text):
    """Lexical tokens worth holding the pipeline accountable for."""

    seen = {}
    for token in fold_tokens(text):
        if len(token.replace("'", "")) >= MIN_MEANINGFUL_CHARS:
            seen.setdefault(token, None)
    return list(seen)


def discard_is_explained(reason):
    reason = str(reason or "")
    return bool(reason) and (
        reason in KNOWN_DISCARD_REASONS or reason.startswith(KNOWN_DISCARD_PREFIXES)
    )


def _snapshot_text(snapshot):
    """The immutable text of a source line.

    ``raw_text`` is the engine output before ``_candidate_from_line`` rewrote the
    shared object; it is the widest evidence available and is preferred when the
    two disagree.
    """

    raw = str(snapshot.get("raw_text") or "")
    text = str(snapshot.get("text") or "")
    return raw if len(compact(raw)) >= len(compact(text)) else text


def _downstream_text(snapshot):
    """The *current* text of a downstream line.

    Never the raw text: a line object is rewritten in place, so its snapshot
    still carries the original ``raw_text`` long after ``text`` was truncated.
    Reading the raw side here would let the evidence of the loss stand in as
    proof that nothing was lost.
    """

    return str(snapshot.get("text") or snapshot.get("raw_text") or "")


def _box(value):
    try:
        x, y, w, h = (int(part) for part in tuple(value)[:4])
    except (TypeError, ValueError):
        return None
    return (x, y, w, h)


def _covers(outer, inner, tolerance=GEOMETRY_TOLERANCE_PX):
    if outer is None or inner is None:
        return True
    ox, oy, ow, oh = outer
    ix, iy, iw, ih = inner
    return (
        ix >= ox - tolerance
        and iy >= oy - tolerance
        and ix + iw <= ox + ow + tolerance
        and iy + ih <= oy + oh + tolerance
    )


def union_box(boxes):
    boxes = [box for box in (_box(item) for item in boxes or []) if box]
    if not boxes:
        return None
    x0 = min(box[0] for box in boxes)
    y0 = min(box[1] for box in boxes)
    x1 = max(box[0] + box[2] for box in boxes)
    y1 = max(box[1] + box[3] for box in boxes)
    return (x0, y0, x1 - x0, y1 - y0)


def resolve_source_lines(member_snapshots, raw_lines):
    """Map each group member back to the immutable snapshot it descends from.

    A line whose geometry moved was re-minted with a new id pointing at its
    parent, so membership alone does not reach the original evidence.  Walking
    the parent chain does, and it is what keeps a truncated derivative from
    becoming its own source of truth.
    """

    raw_by_id = {}
    for snapshot in raw_lines or []:
        raw_by_id.setdefault(str(snapshot.get("line_id") or ""), snapshot)

    resolved = []
    seen_ids = set()
    for snapshot in member_snapshots or []:
        current = snapshot
        visited = {str(current.get("line_id") or "")}
        while True:
            parents = [
                str(parent)
                for parent in current.get("parent_ids") or []
                if str(parent) in raw_by_id and str(parent) not in visited
            ]
            if not parents:
                break
            visited.add(parents[0])
            current = raw_by_id[parents[0]]
        current = raw_by_id.get(str(current.get("line_id") or ""), current)
        line_id = str(current.get("line_id") or "")
        if line_id and line_id in seen_ids:
            continue
        seen_ids.add(line_id)
        resolved.append(current)
    return resolved


def check(
    source_lines,
    *,
    group_id="",
    downstream_text="",
    downstream_lines=(),
    downstream_boxes=(),
    discards=None,
):
    """Compare a group's owned source evidence with its downstream representation.

    ``source_lines`` are immutable provenance snapshots owned by *this* group
    only: page-wide OCR would make a neighbouring balloon's text an expected
    residual for the wrong region.
    """

    result = {
        "status": STATUS_UNAVAILABLE,
        "expected_source_basis": EXPECTED_SOURCE_BASIS,
        "group_id": str(group_id or ""),
        "source_line_ids": [],
        "render_line_ids": [],
        "expected_tokens": [],
        "represented_tokens": [],
        "missing_tokens": [],
        "explained_removed_tokens": [],
        "unexplained_missing_tokens": [],
        "uncovered_source_line_ids": [],
        "source_bbox": None,
        "downstream_bbox": None,
        "reason": "no_provenance_source_lines",
    }
    source_lines = list(source_lines or [])
    if not source_lines:
        return result

    discards = {
        str(line_id): str(reason or "")
        for line_id, reason in dict(discards or {}).items()
    }

    downstream_lines = list(downstream_lines or [])
    result["source_line_ids"] = [
        str(line.get("line_id") or "") for line in source_lines
    ]
    result["render_line_ids"] = [
        str(line.get("line_id") or "") for line in downstream_lines
    ]

    downstream_compact = compact(downstream_text) + "|" + compact(
        " ".join(_downstream_text(line) for line in downstream_lines)
    )

    expected = {}
    for line in source_lines:
        line_id = str(line.get("line_id") or "")
        for token in meaningful_tokens(_snapshot_text(line)):
            expected.setdefault(token, []).append(line_id)

    represented = []
    missing = []
    for token in expected:
        if token in downstream_compact:
            represented.append(token)
        else:
            missing.append(token)

    explained = []
    unexplained = []
    for token in missing:
        owners = expected[token]
        if owners and all(discard_is_explained(discards.get(owner)) for owner in owners):
            explained.append(token)
        else:
            unexplained.append(token)

    source_box = union_box(line.get("bbox") for line in source_lines)
    downstream_box = union_box(
        list(downstream_boxes or [])
        + [line.get("bbox") for line in downstream_lines]
    )
    uncovered = [
        str(line.get("line_id") or "")
        for line in source_lines
        if meaningful_tokens(_snapshot_text(line))
        and not discard_is_explained(discards.get(str(line.get("line_id") or "")))
        and not _covers(downstream_box, _box(line.get("bbox")))
    ]

    result.update(
        {
            "expected_tokens": sorted(expected),
            "represented_tokens": sorted(represented),
            "missing_tokens": sorted(missing),
            "explained_removed_tokens": sorted(explained),
            "unexplained_missing_tokens": sorted(unexplained),
            "uncovered_source_line_ids": uncovered,
            "source_bbox": list(source_box) if source_box else None,
            "downstream_bbox": list(downstream_box) if downstream_box else None,
        }
    )
    if unexplained:
        result["status"] = STATUS_FAIL
        result["reason"] = "source_tokens_lost_without_provenance:" + ",".join(
            sorted(unexplained)[:6]
        )
    elif uncovered:
        result["status"] = STATUS_REVIEW
        result["reason"] = "source_geometry_not_covered:" + ",".join(uncovered[:6])
    else:
        result["status"] = STATUS_PASS
        result["reason"] = "ok"
    return result


def _page_discards(page):
    discards = {}
    for event in page.get("events") or []:
        if event.get("operation") != "line_filtered":
            continue
        line_id = str(event.get("line_id") or "")
        if line_id:
            discards[line_id] = str(event.get("reason") or "")
    return discards


def check_page(page):
    """Run the contract over every group of one recorded page."""

    raw_lines = page.get("raw_lines") or []
    discards = _page_discards(page)
    render_by_group = {}
    for entry in page.get("render_inputs") or []:
        render_by_group.setdefault(str(entry.get("group_id") or ""), entry)

    results = []
    for group in page.get("groups") or []:
        group_id = str(group.get("group_id") or "")
        source_lines = resolve_source_lines(group.get("member_lines"), raw_lines)
        render = render_by_group.get(group_id) or {}
        downstream_text = " ".join(
            part
            for part in (
                str(group.get("text") or ""),
                str(render.get("render_input_text") or ""),
            )
            if part
        )
        boxes = [group.get("bbox")]
        if render:
            boxes.append(render.get("render_input_bbox"))
            boxes.append(render.get("draw_box"))
        result = check(
            source_lines,
            group_id=group_id,
            downstream_text=downstream_text,
            downstream_lines=render.get("render_input_lines") or group.get("member_lines"),
            downstream_boxes=boxes,
            discards=discards,
        )
        result["page"] = page.get("page")
        results.append(result)
    return results


def check_artifact(payload):
    """Run the contract over a loaded ``ocr_line_provenance`` artifact.

    A legacy run has no artifact and reports ``unavailable``; it is never scored
    from its final groups, which would fabricate a pass for exactly the runs the
    evidence cannot speak for.
    """

    if not isinstance(payload, dict) or payload.get("status") == "unavailable":
        return {
            "status": STATUS_UNAVAILABLE,
            "reason": (payload or {}).get("reason", "no_provenance_artifact"),
            "checked_groups": 0,
            "pass": 0,
            "review": 0,
            "fail": 0,
            "unavailable": 0,
            "missing_lexical_tokens": [],
            "groups": [],
        }
    results = [
        result for page in payload.get("pages") or [] for result in check_page(page)
    ]
    return summarize(results)


def summarize(results):
    results = list(results or [])
    missing = sorted(
        {
            token
            for result in results
            for token in result.get("unexplained_missing_tokens") or []
        }
    )
    return {
        "status": (
            STATUS_FAIL
            if any(item["status"] == STATUS_FAIL for item in results)
            else STATUS_REVIEW
            if any(item["status"] == STATUS_REVIEW for item in results)
            else STATUS_PASS
            if results
            else STATUS_UNAVAILABLE
        ),
        "checked_groups": len(results),
        "pass": sum(1 for item in results if item["status"] == STATUS_PASS),
        "review": sum(1 for item in results if item["status"] == STATUS_REVIEW),
        "fail": sum(1 for item in results if item["status"] == STATUS_FAIL),
        "unavailable": sum(
            1 for item in results if item["status"] == STATUS_UNAVAILABLE
        ),
        "missing_lexical_tokens": missing[:50],
        "groups": [
            {
                "page": item.get("page"),
                "group_id": item.get("group_id"),
                "status": item["status"],
                "reason": item.get("reason", ""),
                "unexplained_missing_tokens": item.get(
                    "unexplained_missing_tokens", []
                ),
            }
            for item in results
            if item["status"] in {STATUS_FAIL, STATUS_REVIEW}
        ][:200],
    }


def check_live_group(group, render_lines=None, render_bbox=None):
    """Run the contract for a group being rendered right now.

    Reads the page the recorder is currently filling, so the check runs before
    the renderer consumes the group and again feeds the post-render validator.
    Without an active recorder the answer is ``unavailable``, never a pass.
    """

    import ocr_line_provenance

    page = ocr_line_provenance.current_page()
    if page is None:
        return {
            "status": STATUS_UNAVAILABLE,
            "expected_source_basis": EXPECTED_SOURCE_BASIS,
            "group_id": str(getattr(group, "group_id", "") or ""),
            "reason": "no_active_provenance_recorder",
            "expected_tokens": [],
            "source_line_ids": [],
            "unexplained_missing_tokens": [],
        }
    members = [
        ocr_line_provenance.snapshot_line(line, page["page"])
        for line in (getattr(group, "lines", None) or [])
    ]
    render_snapshots = [
        ocr_line_provenance.snapshot_line(line, page["page"])
        for line in (render_lines or [])
    ]
    boxes = [getattr(group, "box", None), getattr(group, "draw_box", None), render_bbox]
    return check(
        resolve_source_lines(members, page.get("raw_lines")),
        group_id=str(getattr(group, "group_id", "") or ""),
        downstream_text=str(getattr(group, "text", "") or ""),
        downstream_lines=render_snapshots or members,
        downstream_boxes=boxes,
        discards=_page_discards(page),
    )


def expected_physical_tokens(group):
    """Source tokens the rendered region is expected to no longer show.

    The group text is not trustworthy on its own: it is the representation that
    may already have lost the token.  The provenance the group owns is, so the
    expectation is the union, and a token only the provenance remembers is
    exactly the historical blind spot.
    """

    result = check_live_group(group)
    tokens = set(meaningful_tokens(getattr(group, "text", "")))
    tokens.update(result.get("expected_tokens") or [])
    return tokens, result
