from pathlib import Path
from PIL import Image
from research.gemini_hard_clean_fallback import *
from research.gemini_hard_clean_fallback.providers import MockHardCleanProvider

def make_job(tmp, kind="DIALOGUE"):
    src=tmp/"source.png"; Image.new("RGBA",(20,20),(10,20,30,255)).save(src)
    return HardCleanJob("j1","p","r",kind,kind!="SFX","hello",str(src),HardCleanJob.sha256(src),{"x":5,"y":5,"width":4,"height":4})

def test_queue_idempotency_and_masked_composite(tmp_path):
    q=FilesystemQueue(tmp_path/"q"); j=make_job(tmp_path); assert q.enqueue(j); assert not q.enqueue(j)
    result=HardCleanWorker(q,MockHardCleanProvider(tmp_path/"raw")).process_one(); assert result["result"]=="COMPLETED"; assert result["outside_mask_changed_pixels"]==0

def test_sfx_firewall(tmp_path):
    q=FilesystemQueue(tmp_path/"q"); j=make_job(tmp_path,"SFX"); q.enqueue(j); p=MockHardCleanProvider(tmp_path/"raw"); result=HardCleanWorker(q,p).process_one(); assert result["result"]=="SFX_GENERATIVE_ERASE_BLOCKED"; assert not p.calls
