"""Permanent three-page full-pipeline serial/parallel overflow coverage."""
import _test_bootstrap  # noqa: F401
import hashlib, json, os, tempfile, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import cv2, numpy as np
import benchmark_pipeline as bp
import config, local_folder_input
from local_folder_source import LocalFolderChapterAdapter, LocalFolderPolicy
from ocr_balloon import TextCandidate, TextGroup
from ocr_engine import OCRLine

class FixtureTranslator:
    model = "offline-fixture"
    def __init__(self):
        self.initial_calls=[]; self.retry_calls=[]; self.force_cache=False
        self.stats={"api_texts":0,"api_requests":0,"cache_hits":0,"failed_batches":0,"provider_name":"yomu_backend"}
    def translate_many(self, texts, force=False):
        self.initial_calls.extend(texts); self.stats["api_texts"] += len(texts); self.stats["api_requests"] += 1
        # Portuguese markers keep the candidate semantically valid while the
        # forced page is deliberately too long for its narrow region.
        long_valid = "ELE VAI PARA A CASA COM TUDO MELHOR " * 20
        return [long_valid if "FORCE" in t else "ELE VAI" for t in texts]
    def translate_strict(self, text, **_kwargs):
        self.retry_calls.append(text); return "ELE VAI"
    def set_detected_names(self, _): return None

class NoopMonitor:
    def __init__(self,*a,**k): pass
    def start(self): pass
    def set_stage(self,*a,**k): pass
    def set_progress(self,*a,**k): pass
    def register_worker_roles(self,*a,**k): pass
    def stop(self): return {"enabled":False}

class MultiPageFullAB(unittest.TestCase):
    def _run(self, root, workers):
        root.mkdir(parents=True); source=root/"source"; source.mkdir()
        for i in range(3):
            image=np.full((480,640,3),255,np.uint8); image[:,i:i+3,:]=i*40
            self.assertTrue(cv2.imwrite(str(source/f"page{i}.png"), image))
        snaproot=root/"local_sources"; snaproot.mkdir()
        snap=LocalFolderChapterAdapter(LocalFolderPolicy(allowed_roots=(root,))).snapshot(source,snaproot,snapshot_id="fixture")
        out=root/"output"/f"workers-{workers}"; out.mkdir(parents=True)
        ref=local_folder_input.local_source_reference(snap.analysis.source_fingerprint)
        def detect(jobs,_lang,**_kw):
            out={}
            for job in jobs:
                idx=int(job["index"]); text=("THE HERO","FORCE OVERFLOW","THE FRIEND")[idx-1]
                # Page 2 is narrow enough that the first long candidate
                # overflows, but wide enough for the bounded retry to fit.
                width = 1000 if idx == 2 else 260
                height = 300 if idx == 2 else 120
                poly=np.array([[300,220],[300+width,220],[300+width,220+height],[300,220+height]],np.int32)
                line=OCRLine(text=text,raw_text=text,confidence=.99,polygon=poly,box=(300,220,width,height),engine="fixture",page=idx,metadata={})
                out[idx]={"index":idx,"lines":[line],"ocr_metadata":{},"elapsed_seconds":0}
            return out,{"parallel":False,"worker_pids":[]}
        def analyse(_img,lines,page_index=None):
            line=lines[0]; group=TextGroup(group_id=f"G{page_index}",lines=[line],text=line.text,classification="speech",inside_balloon_like_region=True,source_engine="fixture",quality_score=1.0)
            return [TextCandidate(line=line)],[group]
        args=SimpleNamespace(url=ref,max_images=3,full=False,debug_folder=str(out/"debug"),keep_debug=False,fast=True,benchmark=True,force=True,force_download=True,page_indices="",output_folder=str(out),ocr_engine="fixture",use_context=False,session_context_path=str(out/"session.json"),source_candidate_ids=[],local_manifest_path=str(snap.manifest_path),translation_provider="deepl")
        tr=FixtureTranslator(); old=local_folder_input.LOCAL_SNAPSHOT_ROOT; local_folder_input.LOCAL_SNAPSHOT_ROOT=snaproot
        old_marker=os.environ.get("YOMU_CANCEL_TEST_MARKER_DIR"); old_runtime=os.environ.get("TRADUTOR_TEST_RUNTIME_ROOT"); old_output=os.environ.get("TRADUTOR_OUTPUT_ROOT")
        os.environ["YOMU_CANCEL_TEST_MARKER_DIR"] = str(root/"markers"); os.environ["TRADUTOR_TEST_RUNTIME_ROOT"] = str(root); os.environ["TRADUTOR_OUTPUT_ROOT"] = str(root/"output")
        try:
            with mock.patch.object(bp,"_build_translation_provider",return_value=tr), mock.patch.object(bp,"get_translator",return_value=(tr,"eng")), mock.patch.object(bp,"detect_ocr_jobs",side_effect=detect), mock.patch.object(bp,"analyze_image_array",side_effect=analyse), mock.patch.object(bp,"apply_speech_container_reocr",side_effect=lambda a,b,*x,**k:(b,[])), mock.patch.object(bp,"apply_selective_ocr_fallbacks",side_effect=lambda a,b,*x,**k:(b,[])), mock.patch.object(bp,"_grouping_fallback_reason",return_value=""), mock.patch.object(bp,"ResourceMonitor",NoopMonitor), mock.patch.object(bp,"detect_gpu_basic",return_value={}), mock.patch.object(bp,"_git_metadata",return_value={"commit_hash":"fixture","branch":"test"}), mock.patch.multiple(config,OCR_ENGINE="fixture",OCR_FALLBACK_ENGINE="",OCR_HYBRID_FALLBACK=False,SKIP_NO_TEXT_IMAGES=False,ENABLE_DOWNLOAD_CACHE=False,ENABLE_OCR_CACHE=False,ENABLE_IMAGE_PROCESS_CACHE=False,RESOURCE_MONITORING=False,CLASSIFICATION_PROFILING=False,POST_RENDER_OCR_VALIDATION=False,VISUAL_DIFF_VALIDATION=False,TRANSLATION_VALIDATION=True,TRANSLATION_RETRY_ON_MIXED_LANGUAGE=False,TRANSLATE_SFX=False), mock.patch.dict(os.environ,{"PIPELINE_PAGE_WORKERS":str(workers),"TRANSLATION_ENABLED":"true"},clear=False):
                report=bp.run_benchmark(args)
        finally:
            local_folder_input.LOCAL_SNAPSHOT_ROOT=old
            if old_marker is None: os.environ.pop("YOMU_CANCEL_TEST_MARKER_DIR",None)
            else: os.environ["YOMU_CANCEL_TEST_MARKER_DIR"] = old_marker
            if old_runtime is None: os.environ.pop("TRADUTOR_TEST_RUNTIME_ROOT",None)
            else: os.environ["TRADUTOR_TEST_RUNTIME_ROOT"] = old_runtime
            if old_output is None: os.environ.pop("TRADUTOR_OUTPUT_ROOT",None)
            else: os.environ["TRADUTOR_OUTPUT_ROOT"] = old_output
        return report,tr

    def test_three_page_full_serial_parallel_overflow(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); runs=[self._run(root/label,w) for label,w in (("serial",1),("parallel1",2),("parallel2",2))]
            for report,tr in runs:
                self.assertEqual(report.get("status"),"finished")
                self.assertEqual(report.get("quality_validation",{}).get("expected_pdf_pages"),3)
                self.assertTrue(Path(report["pdf_path"]).is_file())
                self.assertGreaterEqual(len(tr.retry_calls),1)
            self.assertEqual(
                runs[0][0].get("page_count_trace", {}).get("processed_logical_pages"),
                [1, 2, 3],
            )

if __name__ == "__main__": unittest.main()
