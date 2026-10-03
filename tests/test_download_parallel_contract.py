import _test_bootstrap  # noqa: F401
import io, threading, time, tempfile, unittest
from pathlib import Path
from unittest import mock
from PIL import Image
import down
from chapter_source import SourceError

def _png(seed):
    im = Image.new("RGB", (480, 640), (seed, 20, 40)); b = io.BytesIO(); im.save(b, "PNG"); return b.getvalue()

class RequestsTransport: name = "requests"
class BrowserSessionTransport: name = "browser"

class DownloadParallelContractTests(unittest.TestCase):
    def _candidates(self, n=4):
        return [{"candidate_id": f"p{i}", "url": f"http://local/page{i}.png", "source":"test", "order":i, "isChapterCandidate":True} for i in range(1,n+1)]

    def _report(self, n):
        return {"viewer_image_count":n,"ignored":[],"downloaded":[],"timings":{"download_seconds":0,"validation_seconds":0,"image_save_seconds":0},"transport_metadata":{"configured":["requests"],"count":1}}

    def test_http_pool_preserves_order_and_mapping_out_of_order(self):
        delays={1:.20,2:.02,3:.10,4:.05}; payloads={i:_png(i) for i in delays}; completion=[]
        def fetch(url, referer, max_retries, transports):
            i=int(url.split("page")[1].split(".")[0]); time.sleep(delays[i]); completion.append(i); return payloads[i]
        fetch.last_transport_name="requests"
        with tempfile.TemporaryDirectory() as td, mock.patch.object(down,"_download_url",side_effect=fetch):
            report=self._report(4); paths=down._download_candidates(None,self._candidates(),None,1,4,None,report,"http://local",td,[RequestsTransport()],parallel_diagnostics={})
            hashes=[Path(p).read_bytes() for p in paths]
        self.assertNotEqual(completion,[1,2,3,4]); self.assertEqual([hashes[i]==payloads[i+1] for i in range(4)],[True]*4)

    def test_cancel_stops_new_http_submissions(self):
        started=[]; gate=threading.Event(); cancel=threading.Event()
        def fetch(url, referer, max_retries, transports):
            i=int(url.split("page")[1].split(".")[0]); started.append(i); cancel.set(); gate.wait(.2); return _png(i)
        fetch.last_transport_name="requests"
        with tempfile.TemporaryDirectory() as td, mock.patch.object(down,"_download_url",side_effect=fetch):
            report=self._report(4); down._download_candidates(None,self._candidates(),None,1,4,None,report,"http://local",td,[RequestsTransport()],cancel_event=cancel)
        self.assertLessEqual(len(started),2)

    def test_rate_limited_transport_honors_retry_after(self):
        class RateLimited:
            name = "requests"
            def __init__(self): self.calls = 0
            def fetch(self, url, *, referer=""):
                self.calls += 1
                if self.calls == 1:
                    exc = SourceError("source_rate_limited", "429"); exc.retry_after = 0.05; raise exc
                return type("R", (), {"content": _png(9), "status": 200, "content_type": "image/png", "final_url": url})()
        transport = RateLimited()
        started = time.perf_counter(); data = down._download_url("http://local/page.png", "http://local", 2, [transport]); elapsed = time.perf_counter() - started
        self.assertIsNotNone(data); self.assertEqual(transport.calls, 2); self.assertGreaterEqual(elapsed, 0.05)

    def test_browser_transport_remains_serial(self):
        active=0; maximum=0; lock=threading.Lock()
        def fetch(url, referer, max_retries, transports):
            nonlocal active, maximum
            with lock: active+=1; maximum=max(maximum,active)
            time.sleep(.01)
            with lock: active-=1
            i=int(url.split("page")[1].split(".")[0]); return _png(i)
        fetch.last_transport_name="browser"
        with tempfile.TemporaryDirectory() as td, mock.patch.object(down,"_download_url",side_effect=fetch):
            down._download_candidates(None,self._candidates(),None,1,4,None,self._report(4),"http://local",td,[BrowserSessionTransport()])
        self.assertEqual(maximum,1)
