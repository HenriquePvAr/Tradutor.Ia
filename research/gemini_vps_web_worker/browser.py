from __future__ import annotations

import base64
import hashlib
import io
import re
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from research.gemini_hard_clean_fallback.contracts import PROMPT_PROFILE, PROMPT_PROFILE_ID


class AuthInteractionRequired(RuntimeError):
    pass


class ProviderArtifactSelectionError(RuntimeError):
    pass


def _select_fresh_generated_image(candidates: list[dict[str, Any]], baseline: set[str]) -> dict[str, Any]:
    """Choose only an image whose source fingerprint appeared after this submission."""
    fresh = [candidate for candidate in candidates
             if candidate.get("fingerprint") not in baseline
             and int(candidate.get("width") or 0) > 0
             and int(candidate.get("height") or 0) > 0]
    if not fresh:
        raise ProviderArtifactSelectionError("fresh_generated_image_not_found")
    return max(fresh, key=lambda candidate: (
        int(candidate["width"]) * int(candidate["height"]), int(candidate["index"])))


class GeminiWebProvider:
    def __init__(self, profile_dir: str | Path, *, headed: bool = True):
        self.profile_dir = str(profile_dir)
        self.headed = headed
        self.playwright = None
        self.browser = None
        self.context = None
        self.cdp_attached = False

    def health(self) -> dict[str, Any]:
        return {"browser": self.context is not None, "profile_dir": self.profile_dir,
                "prompt_profile_id": PROMPT_PROFILE_ID}

    def start(self):
        from playwright.sync_api import sync_playwright
        self.playwright = sync_playwright().start()
        self.context = self.playwright.chromium.launch_persistent_context(
            self.profile_dir, headless=not self.headed, args=["--no-sandbox"])
        return self.context

    def attach_cdp(self, endpoint: str = "http://127.0.0.1:9222"):
        """Attach to an already running official Chrome; never launches a browser."""
        from playwright.sync_api import sync_playwright
        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.connect_over_cdp(endpoint)
        self.context = self.browser.contexts[0] if self.browser.contexts else self.browser.new_context()
        self.cdp_attached = True
        return self.context

    def close(self):
        if self.context:
            self.context.close()
        if self.playwright:
            self.playwright.stop()
        self.context = None
        self.browser = None
        self.cdp_attached = False

    def ensure_session(self, page):
        page.goto("https://gemini.google.com/images", wait_until="domcontentloaded")
        page.wait_for_timeout(1500)
        if "accounts.google.com" in page.url or page.get_by_text(re.compile("fazer login|sign in", re.I)).count():
            raise AuthInteractionRequired("manual Gemini login required")

    def submit_and_recover(self, input_path: str | Path, output_path: str | Path, *, job_id: str) -> dict:
        if self.context is None:
            raise RuntimeError("GeminiWebProvider requires attach_cdp() in VPS worker mode")
        page = self.context.pages[0] if self.context.pages else self.context.new_page()
        self.ensure_session(page)
        uploaded_marker = page.get_by_text(re.compile("Imagem enviada|uploaded", re.I))
        baseline_upload_count = uploaded_marker.count()
        file_input = page.locator('input[type="file"]')
        if file_input.count() == 0:
            page.get_by_role("button", name="Envio e ferramentas", exact=True).click()
            file_input = page.locator('input[type="file"]')
            file_input.first.wait_for(state="attached", timeout=10000)
            image_inputs = page.locator('input[type="file"][accept*="image"]')
            (image_inputs.first if image_inputs.count() else file_input.first).set_input_files(str(input_path))
        else:
            file_input.first.set_input_files(str(input_path))
        for _ in range(60):
            if uploaded_marker.count() > baseline_upload_count:
                break
            page.wait_for_timeout(500)
        else:
            raise ProviderArtifactSelectionError("fresh_upload_confirmation_not_found")
        prompt_box = page.get_by_role("textbox").last
        prompt_box.fill(PROMPT_PROFILE)
        generated_marker = page.get_by_text(re.compile("O Gemini disse|Gemini said", re.I))
        baseline_response_count = generated_marker.count()
        fingerprint_images = """async () => {
          const fingerprint = async value => {
            const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(value));
            return [...new Uint8Array(digest)].map(byte => byte.toString(16).padStart(2, '0')).join('');
          };
          const generated = [...document.images].filter(image => {
            const alt = (image.alt || '').toLowerCase();
            return alt.includes('gerada com ia') || alt.includes('ai generated');
          });
          return await Promise.all(generated.map(async (image, index) => ({
            index, fingerprint: await fingerprint(image.currentSrc || image.src),
            width: image.naturalWidth, height: image.naturalHeight
          })));
        }"""
        baseline_rows = page.evaluate(fingerprint_images)
        baseline_fingerprints = {row["fingerprint"] for row in baseline_rows}
        prompt_box.press("Enter")
        fresh_rows: list[dict[str, Any]] = []
        for _ in range(360):
            current_response_count = generated_marker.count()
            current_rows = page.evaluate(fingerprint_images)
            fresh_rows = [row for row in current_rows if row["fingerprint"] not in baseline_fingerprints
                          and row["width"] > 0 and row["height"] > 0]
            if current_response_count > baseline_response_count and fresh_rows:
                break
            page.wait_for_timeout(500)
        else:
            raise ProviderArtifactSelectionError("new_response_or_fresh_generated_image_not_found")
        selected = _select_fresh_generated_image(fresh_rows, baseline_fingerprints)
        expression = """async ({selectedFingerprint, baselineFingerprints}) => {
          const fingerprint = async value => {
            const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(value));
            return [...new Uint8Array(digest)].map(byte => byte.toString(16).padStart(2, '0')).join('');
          };
          const generated = [...document.images].filter(image => {
            const alt = (image.alt || '').toLowerCase();
            return alt.includes('gerada com ia') || alt.includes('ai generated');
          });
          const candidates = await Promise.all(generated.map(async (image, index) => ({
            image, index, fingerprint: await fingerprint(image.currentSrc || image.src),
            width: image.naturalWidth, height: image.naturalHeight
          })));
          const selected = candidates.find(candidate => candidate.fingerprint === selectedFingerprint &&
            !baselineFingerprints.includes(candidate.fingerprint) && candidate.width > 0 && candidate.height > 0);
          if (!selected) throw new Error('fresh generated image changed before recovery');
          const img = selected.image;
          const src = img.currentSrc || img.src;
          const looksLikeImage = bytes => bytes.length >= 8 &&
            ((bytes[0] === 0x89 && bytes[1] === 0x50 && bytes[2] === 0x4e && bytes[3] === 0x47) ||
             (bytes[0] === 0xff && bytes[1] === 0xd8) ||
             (bytes[0] === 0x52 && bytes[1] === 0x49 && bytes[2] === 0x46 && bytes[3] === 0x46));
          try {
            const response = await fetch(src);
            const bytes = new Uint8Array(await response.arrayBuffer());
            if (response.ok && looksLikeImage(bytes)) {
              let binary = '';
              const chunk = 0x8000;
              for (let i = 0; i < bytes.length; i += chunk) binary += String.fromCharCode(...bytes.subarray(i, i + chunk));
              return {base64: btoa(binary), width: img.naturalWidth, height: img.naturalHeight,
                transport: 'FETCH_BLOB', domIndex: selected.index, freshCandidateCount: candidates.filter(candidate =>
                  !baselineFingerprints.includes(candidate.fingerprint) && candidate.width > 0 && candidate.height > 0).length};
            }
          } catch (_) {}
          const canvas = document.createElement('canvas');
          canvas.width = img.naturalWidth;
          canvas.height = img.naturalHeight;
          canvas.getContext('2d').drawImage(img, 0, 0);
          return {dataUrl: canvas.toDataURL('image/png'), width: img.naturalWidth, height: img.naturalHeight,
            transport: 'CANVAS_DATA_URL', domIndex: selected.index, freshCandidateCount: candidates.filter(candidate =>
              !baselineFingerprints.includes(candidate.fingerprint) && candidate.width > 0 && candidate.height > 0).length};
        }"""
        result = page.evaluate(expression, {"selectedFingerprint": selected["fingerprint"],
                                           "baselineFingerprints": list(baseline_fingerprints)})
        encoded = result.get("base64")
        if encoded is None:
            encoded = result["dataUrl"].split(",", 1)[1]
        data = base64.b64decode(encoded, validate=True)
        try:
            with Image.open(io.BytesIO(data)) as downloaded:
                downloaded.load()
                actual_size = downloaded.size
                rgba = np.asarray(downloaded.convert("RGBA"))
        except Exception as exc:
            raise ProviderArtifactSelectionError("selected_generated_image_failed_decode") from exc
        if actual_size != (int(result["width"]), int(result["height"])):
            raise ProviderArtifactSelectionError("selected_image_dimensions_do_not_match_dom")
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).write_bytes(data)
        alpha = rgba[..., 3]
        return {"job_id": job_id, "bytes": len(data), "width": result["width"], "height": result["height"],
                "image_sha256": hashlib.sha256(data).hexdigest(),
                "asset_selection": {"method": "fresh_generated_dom_asset_v1",
                                    "selected_dom_index": int(result["domIndex"]),
                                    "fresh_candidate_count": int(result["freshCandidateCount"]),
                                    "natural_width": int(result["width"]),
                                    "natural_height": int(result["height"]),
                                    "transport": result["transport"],
                                    "fully_transparent": bool(int(alpha.max()) == 0),
                                    "alpha_min": int(alpha.min()), "alpha_max": int(alpha.max()),
                                    "alpha_mean": round(float(alpha.mean()), 5)}}
