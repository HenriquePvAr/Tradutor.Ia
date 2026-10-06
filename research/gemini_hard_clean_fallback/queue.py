import json, os, shutil
from pathlib import Path
from .contracts import HardCleanJob, JobState

class FilesystemQueue:
    def __init__(self, root):
        self.root=Path(root); [ (self.root/s).mkdir(parents=True,exist_ok=True) for s in ("pending","processing","completed","failed","review_required") ]
    def enqueue(self, job, image=None):
        if (self.root/"pending"/f"{job.job_id}.json").exists() or any((self.root/s/f"{job.job_id}.json").exists() for s in ("processing","completed","failed","review_required")): return False
        job.state=JobState.PENDING.value; (self.root/"pending"/f"{job.job_id}.json").write_text(json.dumps(job.to_dict(),indent=2),encoding="utf-8")
        if image: shutil.copy2(image, self.root/"pending"/f"{job.job_id}.input{Path(image).suffix}")
        return True
    def claim(self):
        files=sorted((self.root/"pending").glob("*.json"))
        if not files: return None
        src=files[0]; dst=self.root/"processing"/src.name
        try: os.replace(src,dst)
        except FileNotFoundError: return None
        job=HardCleanJob.from_path(dst); job.state=JobState.PROCESSING.value; dst.write_text(json.dumps(job.to_dict(),indent=2),encoding="utf-8"); return job
    def finish(self, job, state):
        src=self.root/"processing"/f"{job.job_id}.json"; dst=self.root/state.lower()/src.name
        job.state=state; dst.parent.mkdir(exist_ok=True); dst.write_text(json.dumps(job.to_dict(),indent=2),encoding="utf-8")
        if src.exists(): src.unlink()
