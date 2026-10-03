import _test_bootstrap  # noqa: F401
import io, threading, time, tempfile, statistics
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from unittest import mock
from PIL import Image
import requests
import down
from chapter_source import SourceError

def png(seed):
    im=Image.new("RGB",(480,640),(seed,30,60)); b=io.BytesIO(); im.save(b,"PNG"); return b.getvalue()

class LocalTransport:
    name="requests"
    def fetch(self,url,*,referer=""):
        r=requests.get(url,timeout=5)
        if r.status_code in (429,503):
            e=SourceError("source_rate_limited",str(r.status_code)); e.retry_after=float(r.headers.get("Retry-After","0")); raise e
        if r.status_code != 200: raise SourceError("invalid_image_response",f"status_{r.status_code}")
        return type("R",(),{"content":r.content,"status":r.status_code,"content_type":r.headers.get("Content-Type",""),"final_url":url})()

class LocalHTTPDownloadTests:
    def __init__(self): self.counts={}; self.sequence=[]; self.lock=threading.Lock()
    def server(self, mode="normal"):
        owner=self
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                path=self.path.split("?")[0]; idx=int(path.rsplit("page",1)[1].split(".")[0])
                with owner.lock:
                    owner.counts[idx]=owner.counts.get(idx,0)+1; attempt=owner.counts[idx]
                    if mode=="rate" and idx==2: owner.sequence.append(429 if attempt==1 else 200)
                if mode=="rate" and idx==2 and attempt==1:
                    self.send_response(429); self.send_header("Retry-After","0.05"); self.end_headers(); return
                if mode=="failure" and idx==2:
                    self.send_response(404); self.end_headers(); return
                delay={1:.18,2:.03,3:.10,4:.05}.get(idx,.02); time.sleep(delay)
                body=png(idx); self.send_response(200); self.send_header("Content-Type","image/png"); self.send_header("Content-Length",str(len(body))); self.end_headers(); self.wfile.write(body)
            def log_message(self,*a): pass
        return ThreadingHTTPServer(("127.0.0.1",0),Handler)

def candidates(base,n=4):
    return [{"candidate_id":f"p{i}","url":f"{base}/page{i}.png","source":"http","order":i,"width":480,"height":640,"isChapterCandidate":True} for i in range(1,n+1)]

def report(n): return {"viewer_image_count":n,"ignored":[],"downloaded":[],"timings":{"download_seconds":0,"validation_seconds":0,"image_save_seconds":0},"transport_metadata":{"configured":["requests"],"count":1}}

def test_real_local_http_429_and_order():
    h=LocalHTTPDownloadTests(); s=h.server("rate"); t=threading.Thread(target=s.serve_forever,daemon=True); t.start();
    try:
        with tempfile.TemporaryDirectory() as td:
            r=report(4); paths=down._download_candidates(None,candidates(f"http://127.0.0.1:{s.server_port}"),None,2,4,None,r,"",td,[LocalTransport()])
            assert len(paths)==4 and h.sequence==[429,200]
            assert [Path(p).read_bytes()==png(i) for i,p in enumerate(paths,1)] == [True]*4
    finally: s.shutdown(); s.server_close(); t.join(timeout=1)

def test_real_local_http_failure_skip_order_matches_serial_and_parallel():
    results=[]
    for workers in (1,2):
        h=LocalHTTPDownloadTests(); s=h.server("failure"); t=threading.Thread(target=s.serve_forever,daemon=True); t.start()
        try:
            with tempfile.TemporaryDirectory() as td, mock.patch.object(down,"DOWNLOAD_WORKERS",workers):
                r=report(4); paths=down._download_candidates(None,candidates(f"http://127.0.0.1:{s.server_port}"),None,1,4,None,r,"",td,[LocalTransport()])
                results.append(([Path(p).read_bytes() for p in paths],r))
        finally: s.shutdown(); s.server_close(); t.join(timeout=1)
    assert results[0][0]==results[1][0] and len(results[0][0])==3

def test_real_local_http_benchmark_median():
    med={}
    for workers in (1,2,4):
        samples=[]
        for _ in range(3):
            h=LocalHTTPDownloadTests(); s=h.server("normal"); server_thread=threading.Thread(target=s.serve_forever,daemon=True); server_thread.start()
            try:
                with tempfile.TemporaryDirectory() as td, mock.patch.object(down,"DOWNLOAD_WORKERS",workers):
                    started=time.perf_counter(); down._download_candidates(None,candidates(f"http://127.0.0.1:{s.server_port}"),None,1,4,None,report(4),"",td,[LocalTransport()]); samples.append(time.perf_counter()-started)
            finally: s.shutdown(); s.server_close(); server_thread.join(timeout=1)
        med[workers]=statistics.median(samples)
    print(f"LOCAL_HTTP_MEDIANS={med} SPEEDUP2={med[1]/med[2]:.2f} SPEEDUP4={med[1]/med[4]:.2f}")
