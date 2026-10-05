"""Two owned Gemini pages/chats sharing one Chrome process."""
from __future__ import annotations

import base64
import hashlib
import re
import time
from pathlib import Path

from research.gemini_hard_clean_fallback.contracts import PROMPT_PROFILE
from research.gemini_vps_web_worker.browser import GeminiWebProvider
from chat_correlation import require_fresh_source
from multi_slot_manager import MultiSlotManager
from perf_timing import PhaseTimer


class TwoSlotGeminiProvider(GeminiWebProvider):
    def __init__(self, profile_dir: str | Path, slots: MultiSlotManager):
        super().__init__(profile_dir)
        self.slots = slots

    @staticmethod
    def _target_id(context, page, fallback: str) -> str:
        try:
            info = context.new_cdp_session(page).send("Target.getTargetInfo")
            return str(info["targetInfo"]["targetId"])
        except Exception:
            return fallback

    def attach_cdp(self, endpoint: str = "http://127.0.0.1:9222"):
        super().attach_cdp(endpoint)
        # Keep explicit reserved page handles, including a newly-created blank page.
        pages = list(self.context.pages)
        if not pages:
            pages = [self.context.new_page()]
        if len(pages) < 2:
            pages.append(self.context.new_page())
        # Initial assignment is explicit and persisted; operations never use first/last page dynamically.
        for index, slot_id in enumerate(("slot-1", "slot-2")):
            page = pages[index]
            page_id = self._target_id(self.context, page, f"page-{index + 1}")
            if not self.slots.slots[slot_id].page_id:
                self.slots.register_page(slot_id, page_id)
        return self.context

    def _page_for(self, slot_id: str):
        wanted = self.slots.slots[slot_id].page_id
        for page in self.context.pages:
            current = self._target_id(self.context, page, "")
            if current == wanted:
                return page
        raise RuntimeError(f"page_missing_for_{slot_id}")

    @staticmethod
    def _baseline(page) -> dict:
        return {"messages": page.locator('[role="article"], [data-message-author-role]').count(),
                "sources": tuple(page.locator('img[alt*="gerada com IA"], img[alt*="AI generated"]').evaluate_all("items => items.map(x => x.currentSrc || x.src).filter(Boolean)"))}

    def submit_and_recover(self, input_path: str | Path, output_path: str | Path, *, job_id: str) -> dict:
        request_id = hashlib.sha256(f"{job_id}:{time.time_ns()}".encode()).hexdigest()[:16]
        timer = PhaseTimer()
        timer.mark("slot_acquire_start_at")
        slot = self.slots.acquire(job_id, request_id)
        timer.mark("slot_acquired_at")
        page = self._page_for(slot.slot_id)
        timer.mark("page_ready_at")
        started = time.time()
        rotation_needed = False
        try:
            persisted_chat = slot.chat_id
            persisted_url = slot.chat_url
            if persisted_chat and persisted_url and f"/app/{persisted_chat}" not in page.url:
                page.goto(persisted_url, wait_until="domcontentloaded")
            elif not persisted_chat and "/app/" not in page.url:
                self.ensure_session(page)
            self.slots.observe_chat(slot.slot_id, page.url)
            baseline = self._baseline(page)
            self.slots.transition(slot.slot_id, "UPLOADING")
            timer.mark("upload_start_at")
            files = page.locator('input[type="file"]')
            if files.count() == 0:
                page.get_by_role("button", name="Envio e ferramentas", exact=True).click()
                files = page.locator('input[type="file"]')
                files.first.wait_for(state="attached", timeout=10000)
            files.first.set_input_files(str(input_path))
            timer.mark("upload_end_at")
            self.slots.transition(slot.slot_id, "PROMPTING")
            timer.mark("prompt_start_at")
            page.get_by_text(re.compile("Imagem enviada|uploaded", re.I)).last.wait_for(timeout=30000)
            prompt = page.get_by_role("textbox").last
            prompt.fill("")
            prompt.fill(PROMPT_PROFILE)
            timer.mark("prompt_end_at")
            generation_started = time.time()
            timer.mark("generation_wait_start_at")
            self.slots.transition(slot.slot_id, "GENERATING")
            generation_seq = self.slots.next_generation(slot.slot_id)
            prompt.press("Enter")
            page.get_by_text(re.compile("O Gemini disse|Gemini said", re.I)).last.wait_for(timeout=180000)
            ready = page.locator('img[alt*="gerada com IA"], img[alt*="AI generated"]')
            source = None
            for _ in range(360):
                current = ready.evaluate_all("items => items.map(x => ({src:x.currentSrc || x.src,w:x.naturalWidth})).filter(x => x.src && x.w > 0)")
                try:
                    source = require_fresh_source(baseline["sources"], [item["src"] for item in current])
                    break
                except RuntimeError:
                    page.wait_for_timeout(500)
            if source is None:
                self.slots.mark_rotation(slot.slot_id, "STALE_OUTPUT_OR_GENERATION_STUCK")
                rotation_needed = True
                raise RuntimeError("stale_output_rejected")
            timer.mark("generation_detected_at")
            self.slots.transition(slot.slot_id, "RECOVERING")
            timer.mark("raw_recovery_start_at")
            cdp = self.context.new_cdp_session(page)
            expr = """async (wanted) => { const img=[...document.images].find(x=>(x.currentSrc||x.src)===wanted&&x.naturalWidth>0); if(!img) throw new Error('new image disappeared'); try { const r=await fetch(img.currentSrc||img.src); const b=new Uint8Array(await r.arrayBuffer()); let s=''; for(let i=0;i<b.length;i+=0x8000)s+=String.fromCharCode(...b.subarray(i,i+0x8000)); return {base64:btoa(s),width:img.naturalWidth,height:img.naturalHeight}; } catch(_) { const c=document.createElement('canvas'); c.width=img.naturalWidth;c.height=img.naturalHeight;c.getContext('2d').drawImage(img,0,0); return {dataUrl:c.toDataURL('image/png'),width:img.naturalWidth,height:img.naturalHeight}; } }"""
            evaluated = cdp.send("Runtime.evaluate", {"expression": f"({expr}).call(null, {source!r})", "awaitPromise": True, "returnByValue": True})
            if "exceptionDetails" in evaluated:
                raise RuntimeError("new_output_recovery_failed")
            result = evaluated["result"]["value"]
            encoded = result.get("base64") or result["dataUrl"].split(",", 1)[1]
            data = base64.b64decode(encoded, validate=True)
            Path(output_path).parent.mkdir(parents=True, exist_ok=True)
            Path(output_path).write_bytes(data)
            timer.mark("raw_recovery_end_at")
            self.slots.transition(slot.slot_id, "VALIDATING")
            self.slots.observe_chat(slot.slot_id, page.url)
            return {"job_id": job_id, "slot_id": slot.slot_id, "generation_seq": generation_seq,
                    "chat_reused": bool(generation_seq > 1), "bytes": len(data), "width": result["width"],
                    "height": result["height"], "send_started_at": generation_started,
                    "generation_finished_at": time.time(), "duration_seconds": time.time() - started,
                    "performance_timing": timer.export(),
                    "stale_output_rejected": False}
        except Exception:
            if not rotation_needed:
                self.slots.mark_broken(slot.slot_id, "request_failed")
            raise
        finally:
            current = self.slots.slots[slot.slot_id]
            if current.state == "NEEDS_ROTATION":
                self.slots.release(slot.slot_id, state="NEEDS_ROTATION")
            elif current.state == "BROKEN":
                self.slots.release(slot.slot_id, state="BROKEN")
            else:
                self.slots.release(slot.slot_id)
