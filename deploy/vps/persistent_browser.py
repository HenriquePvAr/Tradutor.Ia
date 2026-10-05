"""Gemini provider wrapper with one owned page/chat and stale-output rejection."""
from __future__ import annotations

import base64
import hashlib
import re
from pathlib import Path

from research.gemini_hard_clean_fallback.contracts import PROMPT_PROFILE
from research.gemini_vps_web_worker.browser import AuthInteractionRequired, GeminiWebProvider
from slot_manager import SlotManager
from chat_correlation import require_fresh_source


class PersistentChatProvider(GeminiWebProvider):
    def __init__(self, profile_dir: str | Path, *, headed: bool = True, state_root: str | Path | None = None):
        super().__init__(profile_dir, headed=headed)
        root = Path(state_root) if state_root else Path(profile_dir).parent / ".runtime" / "chat-slots"
        self.slots = SlotManager(root)

    def health(self) -> dict:
        result = super().health()
        result.update(self.slots.snapshot())
        result["chat_url"] = ""
        result["chat_reuse_enabled"] = True
        return result

    def _page_for_slot(self):
        pages = list(self.context.pages)
        if not pages:
            raise RuntimeError("slot_page_missing")
        registered = self.slots.slot.page_id
        if registered:
            for page in pages:
                page_id = hashlib.sha256((page.url.split("#", 1)[0]).encode()).hexdigest()[:16]
                if page_id == registered or self.slots.slot.chat_id and f"/app/{self.slots.slot.chat_id}" in page.url:
                    return page
            raise RuntimeError("slot_page_not_registered")
        candidates = [page for page in pages if "gemini.google.com" in page.url]
        if len(candidates) != 1:
            raise RuntimeError("slot_page_ambiguous")
        return candidates[0]

    @staticmethod
    def _page_id(page) -> str:
        return hashlib.sha256(page.url.split("#", 1)[0].encode()).hexdigest()[:16]

    @staticmethod
    def _baseline(page) -> dict:
        return {
            "message_count": page.locator('[role="article"], [data-message-author-role]').count(),
            "generated_srcs": tuple(page.locator('img[alt*="gerada com IA"], img[alt*="AI generated"]').evaluate_all("items => items.map(x => x.currentSrc || x.src).filter(Boolean)")),
        }

    def submit_and_recover(self, input_path: str | Path, output_path: str | Path, *, job_id: str) -> dict:
        if self.context is None:
            raise RuntimeError("GeminiWebProvider requires attach_cdp() in VPS worker mode")
        request_id = hashlib.sha256(f"{job_id}:{self.slots.slot.generation_seq + 1}".encode()).hexdigest()[:16]
        self.slots.acquire(job_id, request_id)
        page = None
        try:
            page = self._page_for_slot()
            persisted_chat = self.slots.slot.chat_id
            persisted_url = self.slots.slot.chat_url
            if persisted_chat and persisted_url and f"/app/{persisted_chat}" not in page.url:
                page.goto(persisted_url, wait_until="domcontentloaded")
            elif not persisted_chat and "/app/" not in page.url:
                self.ensure_session(page)
            self.slots.observe_chat(page.url, page_id=self._page_id(page))
            baseline = self._baseline(page)
            self.slots.transition("UPLOADING")
            file_input = page.locator('input[type="file"]')
            if file_input.count() == 0:
                page.get_by_role("button", name="Envio e ferramentas", exact=True).click()
                file_input = page.locator('input[type="file"]')
                file_input.first.wait_for(state="attached", timeout=10000)
            file_input.first.set_input_files(str(input_path))
            self.slots.transition("PROMPTING")
            page.get_by_text(re.compile("Imagem enviada|uploaded", re.I)).last.wait_for(timeout=30000)
            prompt_box = page.get_by_role("textbox").last
            prompt_box.fill("")
            prompt_box.fill(PROMPT_PROFILE)
            self.slots.transition("GENERATING")
            generation_seq = self.slots.next_generation()
            prompt_box.press("Enter")
            page.get_by_text(re.compile("O Gemini disse|Gemini said", re.I)).last.wait_for(timeout=180000)
            ready = page.locator('img[alt*="gerada com IA"], img[alt*="AI generated"]')
            source = None
            for _ in range(360):
                current = ready.evaluate_all("items => items.map(x => ({src:x.currentSrc || x.src, w:x.naturalWidth})).filter(x => x.src && x.w > 0)")
                try:
                    source = require_fresh_source(baseline["generated_srcs"], [item["src"] for item in current])
                    break
                except RuntimeError:
                    pass
                page.wait_for_timeout(500)
            if source is None:
                self.slots.mark_rotation("STALE_OUTPUT_OR_GENERATION_STUCK")
                raise RuntimeError("stale_output_rejected")
            self.slots.transition("RECOVERING")
            cdp = self.context.new_cdp_session(page)
            expression = """async (wanted) => {
              const img = [...document.images].find(image => (image.currentSrc || image.src) === wanted && image.naturalWidth > 0);
              if (!img) throw new Error('new generated image disappeared');
              try {
                const response = await fetch(img.currentSrc || img.src);
                const bytes = new Uint8Array(await response.arrayBuffer());
                let binary = ''; const chunk = 0x8000;
                for (let i = 0; i < bytes.length; i += chunk) binary += String.fromCharCode(...bytes.subarray(i, i + chunk));
                return {base64: btoa(binary), width: img.naturalWidth, height: img.naturalHeight};
              } catch (_) {
                const canvas = document.createElement('canvas');
                canvas.width = img.naturalWidth; canvas.height = img.naturalHeight;
                canvas.getContext('2d').drawImage(img, 0, 0);
                return {dataUrl: canvas.toDataURL('image/png'), width: img.naturalWidth, height: img.naturalHeight};
              }
            }"""
            evaluation = cdp.send("Runtime.evaluate", {"expression": f"({expression}).call(null, {source!r})", "awaitPromise": True, "returnByValue": True})
            if "exceptionDetails" in evaluation:
                raise RuntimeError("new output recovery failed")
            result = evaluation["result"]["value"]
            encoded = result.get("base64") or result["dataUrl"].split(",", 1)[1]
            data = base64.b64decode(encoded, validate=True)
            Path(output_path).parent.mkdir(parents=True, exist_ok=True)
            Path(output_path).write_bytes(data)
            self.slots.transition("VALIDATING")
            self.slots.observe_chat(page.url, page_id=self._page_id(page))
            return {"job_id": job_id, "bytes": len(data), "width": result["width"], "height": result["height"], "generation_seq": generation_seq, "chat_reused": generation_seq > 1, "stale_output_rejected": False}
        except AuthInteractionRequired:
            raise
        finally:
            self.slots.release()
