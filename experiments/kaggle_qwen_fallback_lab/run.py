from __future__ import annotations
import argparse, hashlib, json, os, shutil, subprocess, tempfile, time
from pathlib import Path

MODEL = "Qwen/Qwen3-VL-8B-Instruct"
REQUIRED = {"schema_version","status","image_sha256","model","prompt","answer","runtime"}

def is_m20(path: Path) -> bool:
    return any("m20" in part.lower() for part in path.parts) or "m20" in path.stem.lower()

def run(cmd, cwd=None):
    return subprocess.run(cmd, cwd=cwd, check=True, text=True, capture_output=True)

def wait_kernel(kaggle, slug, timeout=3600):
    deadline=time.time()+timeout
    while time.time() < deadline:
        p=run([kaggle,"kernels","status",slug])
        text=(p.stdout+p.stderr).lower()
        if any(x in text for x in ("complete", "error", "failed", "cancelled")):
            if "complete" not in text: raise RuntimeError("Kaggle kernel failed: "+p.stdout.strip())
            return p.stdout.strip()
        time.sleep(20)
    raise TimeoutError("Kaggle kernel timeout")

def main(argv=None):
    ap=argparse.ArgumentParser(description="Experimental private Kaggle Qwen lab")
    ap.add_argument("--image", required=True); ap.add_argument("--prompt", required=True)
    ap.add_argument("--kaggle-slug", required=True); ap.add_argument("--output", default="reports/kaggle_qwen_fallback_lab.json")
    ap.add_argument("--dry-run", action="store_true"); ap.add_argument("--keep-staging", action="store_true")
    a=ap.parse_args(argv); image=Path(a.image).resolve()
    if not image.is_file(): raise SystemExit(f"image not found: {image}")
    if is_m20(image): raise SystemExit("refusing M20 image: fallback lab requires NON-M20 input")
    if "/" not in a.kaggle_slug or any(x in a.kaggle_slug for x in (" ","\\")): raise SystemExit("kaggle slug must be owner/name")
    report={"status":"planned","model":MODEL,"image":str(image),"kaggle_slug":a.kaggle_slug,"cleanup":{},"started_at":time.time()}
    staging=Path(tempfile.mkdtemp(prefix="qwen-kaggle-lab-")); dataset=staging/"dataset"; dataset.mkdir()
    code=0
    try:
        digest=hashlib.sha256(image.read_bytes()).hexdigest(); shutil.copy2(image, dataset/("input"+image.suffix.lower()))
        (dataset/"dataset-metadata.json").write_text(json.dumps({"title":"qwen-fallback-lab","id":a.kaggle_slug,"licenses":[{"name":"CC0-1.0"}],"private":True}), encoding="utf-8")
        (dataset/"prompt.txt").write_text(a.prompt, encoding="utf-8")
        report.update({"image_sha256":digest,"staging":str(staging)})
        if a.dry_run:
            report["status"]="dry_run"
            if not a.keep_staging:
                shutil.rmtree(staging, ignore_errors=True); report["cleanup"]["staging_deleted"]=True
            return finish(report,a.output,0)
        run(["kaggle","datasets","create","-p",str(dataset),"--dir-mode","zip"])
        report["dataset_created"]=True
        nb=staging/"kernel"; nb.mkdir(); shutil.copy2(Path(__file__).with_name("worker.py"), nb/"worker.py")
        owner, dataset_name=a.kaggle_slug.split("/",1); kernel_name=dataset_name+"-job"; kernel_slug=owner+"/"+kernel_name
        (nb/"kernel-metadata.json").write_text(json.dumps({"id":kernel_slug,"title":kernel_name,"code_file":"worker.py","language":"python","kernel_type":"script","is_private":"true","enable_gpu":"true","enable_internet":"true","dataset_sources":[a.kaggle_slug]}), encoding="utf-8")
        run(["kaggle","kernels","push","-p",str(nb)]); report["job_submitted"]=True
        report["kernel_status"]=wait_kernel("kaggle",kernel_slug)
        run(["kaggle","kernels","output",a.kaggle_slug+"-job","-p",str(staging/"output")])
        result=json.loads((staging/"output"/"result.json").read_text(encoding="utf-8"))
        if set(result) < REQUIRED or result["image_sha256"] != digest or result["model"] != MODEL: raise ValueError("invalid result.json contract")
        report.update({"status":"ok","result":result})
    except subprocess.CalledProcessError as e:
        code=1; report.update({"status":"failed","error":str(e),"command_stderr":e.stderr[-2000:] if e.stderr else "","command_stdout":e.stdout[-2000:] if e.stdout else ""})
    except Exception as e:
        code=1; report.update({"status":"failed","error":str(e)})
    finally:
        try:
            if not a.dry_run and report.get("dataset_created"):
                run(["kaggle","datasets","delete",a.kaggle_slug,"--yes"]); report["cleanup"]["remote_dataset_deleted"]=True
        except Exception as e: report["cleanup"]["remote_dataset_delete_error"]=str(e)
        if not a.keep_staging: shutil.rmtree(staging, ignore_errors=True); report["cleanup"]["staging_deleted"]=True
    return finish(report,a.output,code)

def finish(report, path, code):
    report["elapsed_seconds"]=round(time.time()-report.get("started_at",time.time()),3)
    out=Path(path); out.parent.mkdir(parents=True,exist_ok=True); out.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8"); print(json.dumps(report,ensure_ascii=False,indent=2)); return code

if __name__ == "__main__": raise SystemExit(main())
