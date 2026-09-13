import json, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request, urlopen
from yomu_backend_provider import BatchItem, BatchRequest, BatchResponse
from server_translation_authority import ServerTranslationAuthority


class Provider:
    def __init__(self): self.calls = 0
    def __call__(self, req, reservation):
        self.calls += 1
        return BatchResponse(req.request_id, "completed", tuple(BatchItem(i.item_id, "T:" + i.text) for i in req.items), reservation)


def test_http_batch_replay_and_unicode():
    provider = Provider(); authority = ServerTranslationAuthority(provider)
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            req = BatchRequest(body["request_id"], body["job_id"], body["source_lang"], body["target_lang"], tuple(BatchItem(**i) for i in body["items"]))
            out = authority.execute(user_id="user", request=req, reservation_id="res")
            data = json.dumps({"request_id": out.request_id, "status": out.status, "items": [i.__dict__ for i in out.items]}).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(data)
        def log_message(self, *_): pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler); threading.Thread(target=server.serve_forever, daemon=True).start()
    payload = {"request_id":"r", "job_id":"j", "source_lang":"JA", "target_lang":"PT-BR", "items":[{"item_id":"a","text":"Olá"},{"item_id":"b","text":"Olá"}]}
    try:
        for _ in range(2):
            req = Request(f"http://127.0.0.1:{server.server_port}", data=json.dumps(payload, ensure_ascii=False).encode(), method="POST", headers={"Content-Type":"application/json"})
            result = json.loads(urlopen(req).read()); assert [i["item_id"] for i in result["items"]] == ["a", "b"]
    finally: server.shutdown()
    assert provider.calls == 1
