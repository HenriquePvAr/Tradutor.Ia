import argparse
import html
import json
import re
from pathlib import Path

from PIL import Image

from json_utils import to_json_safe


def _load_json(path):
    with Path(path).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _failure_items(report):
    items = []
    for page in report.get("pages", []):
        page_index = int(page.get("original_index") or page.get("index") or 0)
        for failure in page.get("visual_validation_failures", []):
            item = dict(failure)
            item["page"] = page_index
            item["source_page_path"] = page.get("image_path")
            item["final_page_path"] = page.get("output_path")
            items.append(item)
    return items


def _normalized_text(value):
    return re.sub(r"[^A-Z0-9]+", "", str(value or "").upper())


def _review_category(item):
    classification = str(item.get("classification") or "unknown").lower()
    original = _normalized_text(item.get("text"))
    translation = _normalized_text(item.get("translation"))
    metrics = item.get("background_metrics") or {}
    white_ratio = float(metrics.get("white_pixel_ratio", 1.0))
    saturation = float(metrics.get("saturation_mean", 0.0))
    token_count = len(re.findall(r"[A-Z0-9']+", str(item.get("text") or "").upper()))
    embedded_short_art_text = (
        1 <= token_count <= 3
        and len(original) <= 24
        and white_ratio < 0.20
        and saturation >= 30.0
    )
    if classification in {"sfx", "decorative"}:
        return "A", "efeito visual/decorativo preservado"
    if embedded_short_art_text:
        return "A", "texto curto incorporado à arte, SFX ou elemento do cenário"
    if original and original == translation:
        return "A", "conteúdo não exige tradução (por exemplo, nome próprio)"
    if classification in {"speech", "thought", "narration"}:
        return "B", "fala, pensamento ou narração necessária para a leitura"
    return "B", "texto semântico não classificado com segurança"


def _visual_region(item):
    metrics = item.get("background_metrics") or {}
    white_ratio = float(metrics.get("white_pixel_ratio", 0.0))
    brightness = float(metrics.get("brightness_mean", 0.0))
    saturation = float(metrics.get("saturation_mean", 255.0))
    dark_ratio = float(metrics.get("dark_pixel_ratio", 1.0))
    classification = str(item.get("classification") or "unknown").lower()
    if (
        classification in {"speech", "thought", "narration"}
        and white_ratio >= 0.75
        and brightness >= 205
        and saturation <= 28
        and dark_ratio <= 0.18
    ):
        return "balão/caixa com interior branco dominante"
    return str(item.get("background_type") or "unknown")


def _safe_crop(image, bbox, margin=24):
    x, y, width, height = [int(value) for value in bbox]
    left = max(0, x - margin)
    top = max(0, y - margin)
    right = min(image.width, x + width + margin)
    bottom = min(image.height, y + height + margin)
    return image.crop((left, top, right, bottom))


def _save_assets(items, assets_dir):
    assets_dir.mkdir(parents=True, exist_ok=True)
    page_assets = {}
    for item in items:
        page = item["page"]
        region_id = str(item.get("region_id") or item.get("id") or "region")
        source_path = Path(item["source_page_path"])
        final_path = Path(item["final_page_path"])
        if page not in page_assets:
            with Image.open(final_path) as image:
                thumb = image.convert("RGB")
                thumb.thumbnail((520, 820), Image.Resampling.LANCZOS)
                name = f"page_{page:03d}.jpg"
                thumb.save(assets_dir / name, quality=84, optimize=True)
                page_assets[page] = name

        bbox = item.get("bounding_box") or [0, 0, 1, 1]
        stem = f"page_{page:03d}_{region_id.lower()}"
        with Image.open(source_path) as image:
            original_crop = _safe_crop(image.convert("RGB"), bbox)
            original_name = f"{stem}_original.jpg"
            original_crop.save(assets_dir / original_name, quality=90, optimize=True)
        with Image.open(final_path) as image:
            final_crop = _safe_crop(image.convert("RGB"), bbox)
            final_name = f"{stem}_final.jpg"
            final_crop.save(assets_dir / final_name, quality=90, optimize=True)
        item["assets"] = {
            "page_thumbnail": page_assets[page],
            "original_crop": original_name,
            "final_crop": final_name,
        }


def _render_html(payload, output_path):
    summary = payload["summary"]
    cards = []
    for item in payload["groups"]:
        category = item["category"]
        badge_class = "badge-a" if category == "A" else "badge-b"
        status_class = "status-ok" if item["resolved"] else "status-review"
        assets = item["assets"]
        cards.append(
            f"""
            <article class="review-card">
              <header class="card-header">
                <div>
                  <p class="overline">Página {item['page']:03d} · {html.escape(item['region_id'])}</p>
                  <h2>{html.escape(item['original_text'])}</h2>
                </div>
                <span class="badge {badge_class}">Categoria {category}</span>
              </header>
              <div class="visual-grid">
                <figure class="page-figure">
                  <img src="manual_review_assets/{assets['page_thumbnail']}" alt="Página {item['page']} após a correção" loading="lazy">
                  <figcaption>página final</figcaption>
                </figure>
                <div class="crop-stack">
                  <figure>
                    <img src="manual_review_assets/{assets['original_crop']}" alt="Recorte original do grupo" loading="lazy">
                    <figcaption>antes</figcaption>
                  </figure>
                  <figure>
                    <img src="manual_review_assets/{assets['final_crop']}" alt="Recorte corrigido do grupo" loading="lazy">
                    <figcaption>depois</figcaption>
                  </figure>
                </div>
              </div>
              <dl class="facts">
                <div><dt>Tradução tentada</dt><dd>{html.escape(item['translation'] or '—')}</dd></div>
                <div><dt>Tipo</dt><dd>{html.escape(item['classification'])}</dd></div>
                <div><dt>Região visual</dt><dd>{html.escape(item['visual_region'])}</dd></div>
                <div><dt>Motivo da reversão</dt><dd>{html.escape(item['reversion_reason'])}</dd></div>
                <div><dt>Impacto narrativo</dt><dd>{html.escape(item['importance'])}</dd></div>
                <div><dt>Fallback OCR</dt><dd>{html.escape(item['fallback_description'])}</dd></div>
              </dl>
              <footer class="recommendation">
                <span class="status {status_class}">{html.escape(item['status'])}</span>
                <p><strong>Recomendação:</strong> {html.escape(item['recommendation'])}</p>
              </footer>
            </article>
            """
        )

    document = f"""<!doctype html>
<html lang="pt-BR">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <link rel="icon" href="data:,">
  <title>Revisão manual · Lookism EP 50</title>
  <style>
    :root {{
      --paper: oklch(97% 0.012 78);
      --ink: oklch(20% 0.025 45);
      --muted: oklch(48% 0.025 55);
      --line: oklch(83% 0.025 70);
      --surface: oklch(99% 0.006 80);
      --accent: oklch(62% 0.19 36);
      --safe: oklch(55% 0.12 155);
      --space-1: 4px; --space-2: 8px; --space-3: 12px; --space-4: 16px;
      --space-6: 24px; --space-8: 32px; --space-12: 48px; --space-16: 64px;
      --radius-sm: 4px; --radius-md: 8px; --radius-lg: 16px;
      --font-display: Georgia, "Times New Roman", serif;
      --font-body: "Segoe UI", Arial, sans-serif;
      --font-mono: Consolas, "Courier New", monospace;
    }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; color: var(--ink); background: var(--paper); font: 15px/1.6 var(--font-body); }}
    img {{ display: block; max-width: 100%; height: auto; }}
    .hero {{ padding: var(--space-16) max(var(--space-6), 6vw) var(--space-12); border-bottom: 1px solid var(--line); }}
    .hero-inner {{ max-width: 1120px; margin: 0 auto; display: grid; grid-template-columns: minmax(0, 1.4fr) minmax(240px, .6fr); gap: var(--space-12); align-items: end; }}
    .kicker, .overline {{ margin: 0 0 var(--space-2); font: 700 11px/1.3 var(--font-mono); letter-spacing: .12em; text-transform: uppercase; color: var(--accent); }}
    h1 {{ margin: 0; max-width: 13ch; font: 700 clamp(42px, 8vw, 88px)/.94 var(--font-display); letter-spacing: -.045em; text-wrap: balance; }}
    .lede {{ max-width: 52ch; margin: var(--space-6) 0 0; color: var(--muted); font-size: 17px; }}
    .score {{ border-left: 4px solid var(--safe); padding-left: var(--space-6); }}
    .score strong {{ display: block; font: 700 48px/1 var(--font-display); }}
    .score span {{ color: var(--muted); }}
    main {{ max-width: 1120px; margin: 0 auto; padding: var(--space-12) max(var(--space-4), 4vw) var(--space-16); }}
    .summary {{ display: flex; flex-wrap: wrap; gap: var(--space-3); margin-bottom: var(--space-12); }}
    .summary span {{ padding: var(--space-2) var(--space-4); background: var(--surface); border: 1px solid var(--line); border-radius: 999px; }}
    .review-list {{ display: grid; gap: var(--space-8); }}
    .review-card {{ background: var(--surface); border: 1px solid var(--line); border-radius: var(--radius-lg); overflow: hidden; }}
    .card-header {{ display: flex; justify-content: space-between; gap: var(--space-6); padding: var(--space-6); border-bottom: 1px solid var(--line); }}
    h2 {{ margin: 0; max-width: 28ch; font: 700 clamp(24px, 4vw, 38px)/1.08 var(--font-display); letter-spacing: -.02em; overflow-wrap: anywhere; }}
    .badge, .status {{ align-self: start; padding: var(--space-1) var(--space-3); border-radius: 999px; font: 700 11px/1.5 var(--font-mono); text-transform: uppercase; letter-spacing: .06em; white-space: nowrap; }}
    .badge-a {{ color: var(--safe); border: 1px solid color-mix(in oklch, var(--safe), white 55%); }}
    .badge-b {{ color: var(--accent); border: 1px solid color-mix(in oklch, var(--accent), white 55%); }}
    .visual-grid {{ display: grid; grid-template-columns: minmax(180px, .7fr) minmax(0, 1.3fr); gap: var(--space-4); padding: var(--space-6); background: oklch(94% 0.012 78); }}
    figure {{ margin: 0; }}
    figure img {{ width: 100%; background: white; border: 1px solid var(--line); }}
    figcaption {{ margin-top: var(--space-2); font: 700 10px/1.3 var(--font-mono); letter-spacing: .1em; text-transform: uppercase; color: var(--muted); }}
    .page-figure img {{ max-height: 440px; object-fit: contain; }}
    .crop-stack {{ display: grid; grid-template-columns: 1fr 1fr; gap: var(--space-4); align-items: start; }}
    .crop-stack img {{ min-height: 140px; max-height: 240px; object-fit: contain; }}
    .facts {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); margin: 0; padding: var(--space-6); gap: var(--space-4) var(--space-8); }}
    .facts div {{ border-top: 1px solid var(--line); padding-top: var(--space-3); }}
    dt {{ font: 700 10px/1.3 var(--font-mono); letter-spacing: .1em; text-transform: uppercase; color: var(--muted); }}
    dd {{ margin: var(--space-1) 0 0; }}
    .recommendation {{ display: flex; gap: var(--space-4); align-items: center; padding: var(--space-4) var(--space-6); border-top: 1px solid var(--line); }}
    .recommendation p {{ margin: 0; }}
    .status-ok {{ color: white; background: var(--safe); }}
    .status-review {{ color: white; background: var(--accent); }}
    @media (max-width: 700px) {{
      .hero {{ padding-top: var(--space-12); }}
      .hero-inner, .visual-grid {{ grid-template-columns: 1fr; }}
      .score {{ margin-left: var(--space-8); }}
      .card-header, .recommendation {{ align-items: flex-start; flex-direction: column; }}
      .facts {{ grid-template-columns: 1fr; }}
      .crop-stack {{ grid-template-columns: 1fr 1fr; }}
    }}
  </style>
</head>
<body>
  <header class="hero">
    <div class="hero-inner">
      <div>
        <p class="kicker">Tradutor.Ia · auditoria visual</p>
        <h1>Treze grupos, sem esconder o risco.</h1>
        <p class="lede">Registro dos grupos inicialmente revertidos, com o recorte original, a saída corrigida e a decisão A/B. A regra de produção permanece genérica: interior branco dominante e evidência visual de balão.</p>
      </div>
      <div class="score"><strong>{summary['resolved']}/{summary['total']}</strong><span>grupos resolvidos ou preservados intencionalmente</span></div>
    </div>
  </header>
  <main>
    <div class="summary" aria-label="Resumo">
      <span>{summary['category_a']} Categoria A</span>
      <span>{summary['category_b']} Categoria B</span>
      <span>{summary['corrected']} corrigidos</span>
      <span>{summary['intentionally_preserved']} preservado de propósito</span>
    </div>
    <section class="review-list" aria-label="Grupos revisados">{''.join(cards)}</section>
  </main>
</body>
</html>"""
    output_path.write_text(document, encoding="utf-8")


def generate_report(output_folder, source_report, current_report):
    output_folder = Path(output_folder).resolve()
    source = _load_json(source_report)
    current = _load_json(current_report)
    source_items = _failure_items(source)
    current_failure_keys = {
        (item["page"], str(item.get("region_id") or item.get("id") or ""))
        for item in _failure_items(current)
    }

    reviewed = []
    for item in source_items:
        category, category_reason = _review_category(item)
        key = (item["page"], str(item.get("region_id") or item.get("id") or ""))
        resolved = key not in current_failure_keys
        same_text = _normalized_text(item.get("text")) == _normalized_text(item.get("translation"))
        if category == "A":
            status = "preservado intencionalmente"
            recommendation = "Manter o conteúdo original; não há ganho semântico em redesenhá-lo."
            importance = "baixo impacto de tradução; conteúdo permanece compreensível"
        else:
            status = "corrigido" if resolved else "revisão pendente"
            recommendation = (
                "Traduzir dentro do enclosure branco detectado, preservando contorno e arte."
                if resolved
                else "Usar fallback OCR regional e manter o original se a limpeza segura falhar."
            )
            importance = "alto; necessário para acompanhar fala, pensamento ou narração"

        reviewed.append(
            {
                "page": item["page"],
                "id": str(item.get("id") or ""),
                "region_id": str(item.get("region_id") or item.get("id") or ""),
                "original_text": str(item.get("text") or ""),
                "translation": str(item.get("translation") or ""),
                "classification": str(item.get("classification") or "unknown"),
                "visual_region": _visual_region(item),
                "reversion_reason": str(
                    (item.get("visual_validation") or {}).get("reason")
                    or "visual_validation_failed"
                ),
                "category": category,
                "category_reason": category_reason,
                "importance": importance,
                "recommendation": recommendation,
                "fallback_description": (
                    f"sim · {item.get('source_engine') or 'motor alternativo'}"
                    if item.get("fallback_used")
                    else "não utilizado neste grupo"
                ),
                "resolved": resolved,
                "same_text_after_translation": same_text,
                "status": status,
                "bounding_box": item.get("bounding_box") or [0, 0, 1, 1],
                "source_page_path": item.get("source_page_path"),
                "final_page_path": item.get("final_page_path"),
            }
        )

    _save_assets(reviewed, output_folder / "manual_review_assets")
    summary = {
        "total": len(reviewed),
        "category_a": sum(item["category"] == "A" for item in reviewed),
        "category_b": sum(item["category"] == "B" for item in reviewed),
        "corrected": sum(item["category"] == "B" and item["resolved"] for item in reviewed),
        "intentionally_preserved": sum(item["category"] == "A" for item in reviewed),
        "resolved": sum(item["resolved"] or item["category"] == "A" for item in reviewed),
        "current_gate_passed": bool(
            (current.get("summary") or {}).get("quality_validation", {}).get("passed")
        ),
    }
    payload = {"summary": summary, "groups": reviewed}
    json_path = output_folder / "manual_review_report.json"
    html_path = output_folder / "manual_review_report.html"
    json_path.write_text(
        json.dumps(to_json_safe(payload), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    _render_html(payload, html_path)
    return json_path, html_path


def main():
    parser = argparse.ArgumentParser(description="Gera revisão visual dos grupos revertidos.")
    parser.add_argument("--output-folder", required=True)
    parser.add_argument("--source-report", required=True)
    parser.add_argument("--current-report", required=True)
    args = parser.parse_args()
    json_path, html_path = generate_report(
        args.output_folder,
        args.source_report,
        args.current_report,
    )
    print(json_path)
    print(html_path)


if __name__ == "__main__":
    main()
