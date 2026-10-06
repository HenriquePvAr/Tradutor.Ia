from pathlib import Path
from PIL import Image, ImageChops
from .contracts import JobState

class HardCleanWorker:
    def __init__(self, queue, provider, max_attempts=2): self.queue=queue; self.provider=provider; self.max_attempts=max_attempts
    def process_one(self, dry_run=False):
        job=self.queue.claim()
        if not job: return None
        if job.content_type.upper()=="SFX" or not job.erase_eligible:
            self.queue.finish(job,JobState.REVIEW_REQUIRED.value); return {"job_id":job.job_id,"result":"SFX_GENERATIVE_ERASE_BLOCKED"}
        image=Path(job.input_path)
        try:
            if dry_run: self.queue.finish(job,JobState.REVIEW_REQUIRED.value); return {"job_id":job.job_id,"result":"DRY_RUN"}
            raw=self.provider.clean_text_region(image,job.target_text,job.authorized_region,job.prompt_profile,job.job_id)
            original=Image.open(image).convert("RGBA"); generated=Image.open(raw).convert("RGBA")
            source_ratio=original.width / original.height
            generated_ratio=generated.width / generated.height
            ratio_delta=abs(source_ratio-generated_ratio) / source_ratio
            # A generative image with a materially different canvas is not aligned.
            # Never force-resize it into the source: that can hide a full recreation.
            if ratio_delta > 0.05:
                self.queue.finish(job,JobState.REVIEW_REQUIRED.value)
                return {"job_id":job.job_id,"result":"REVIEW_REQUIRED","reason":"ALIGNMENT_ASPECT_RATIO_FAIL","source_size":original.size,"generated_size":generated.size,"ratio_delta":ratio_delta}
            if generated.size!=original.size:
                generated=generated.resize(original.size)
            mask=Image.new("L",original.size,0); r=job.authorized_region; mask.paste(255,(r["x"],r["y"],r["x"]+r["width"],r["y"]+r["height"]))
            final=Image.composite(generated,original,mask); out=self.queue.root/"completed"/f"{job.job_id}.final.png"; final.save(out)
            outside=ImageChops.difference(final,original); outside.paste(0,mask=mask); changed=sum(1 for p in outside.getdata() if p!=(0,0,0,0))
            result="COMPLETED" if changed==0 else "REVIEW_REQUIRED"; self.queue.finish(job,result); return {"job_id":job.job_id,"result":result,"outside_mask_changed_pixels":changed,"raw":str(raw),"final":str(out)}
        except Exception as exc:
            job.attempt+=1; self.queue.finish(job,JobState.REVIEW_REQUIRED.value if job.attempt>=self.max_attempts else JobState.FAILED.value); return {"job_id":job.job_id,"result":"FAILED","error":str(exc)}
