"""HTTP 接口层：标准库 http.server 实现的 JSON 路由。

路由全部返回 JSON；业务错误映射为 4xx/409，不泄露堆栈。
"""
from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from engine import EngineError
from service import Service
from store import Store, StoreError

SERVICE_NAME = '公司行动核算服务'


def health_payload() -> dict[str, str]:
    return {"status": "ok", "service": SERVICE_NAME}


def build_service(runtime_dir: str | None = None,
                  holidays: set[str] | None = None) -> Service:
    root = runtime_dir or os.getenv("RUNTIME_DIR", ".runtime")
    if holidays is None:
        holidays = set()
        holidays_file = os.getenv("HOLIDAYS_FILE")
        if holidays_file and os.path.exists(holidays_file):
            with open(holidays_file, encoding="utf-8") as fh:
                holidays = set(json.load(fh))
    return Service(Store(root), holidays=holidays)


class RequestHandler(BaseHTTPRequestHandler):
    service: Service = None  # 由 create_server 注入

    # ------------------------------------------------------------ 基础工具
    def _json(self, code: int, obj) -> None:
        body = json.dumps(obj, ensure_ascii=False, indent=1).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            raise ValueError("请求体不是合法 JSON")

    def _query(self) -> dict:
        return {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        q = self._query()
        svc = self.service
        try:
            if method == "GET" and path == "/health":
                return self._json(200, health_payload())
            if method == "POST" and path == "/ingest":
                body = self._body()
                messages = body.get("messages", [body] if "kind" in body else [])
                if not messages:
                    raise ValueError("请求体需包含 messages 列表")
                result = svc.ingest(messages)
                if q.get("rebuild") == "true":
                    result["rebuild"] = svc.rebuild()
                return self._json(200, result)
            if method == "POST" and path == "/rebuild":
                return self._json(200, svc.rebuild())
            if method == "GET" and path == "/versions":
                return self._json(200, {"versions": svc.versions()})
            if method == "GET" and path == "/positions":
                return self._json(200, svc.positions(q["account"], q["date"]))
            if method == "GET" and path == "/lineage":
                return self._json(200, svc.lineage(q["account"], q["security"], q["date"]))
            if method == "GET" and path == "/pending":
                return self._json(200, {"pending": svc.pending()})
            if method == "GET" and path == "/corporate-actions":
                return self._json(200, {"actions": svc.corporate_actions(q.get("security"))})
            if method == "GET" and path == "/fx":
                return self._json(200, svc.fx(q["pair"], q["date"]))
            if method == "GET" and path == "/realized":
                return self._json(200, {"realized": svc.realized(q["account"])})
            if method == "POST" and path == "/snapshots":
                body = self._body()
                return self._json(201, svc.publish_snapshot(body["account"], body["date"]))
            if method == "GET" and path == "/snapshots":
                return self._json(200, {"snapshots": svc.list_snapshots()})
            if method == "GET" and path.startswith("/snapshots/"):
                return self._json(200, svc.get_snapshot(path.split("/")[-1]))
            if method == "GET" and path == "/export/lineage":
                return self._json(200, svc.export_lineage(q["account"], q["security"], q["date"]))
            if method == "POST" and path == "/verify":
                return self._json(200, Service.verify_export(self._body()))
            return self._json(404, {"error": "not_found", "path": path})
        except EngineError as exc:
            return self._json(409, {"error": "engine_error", "detail": str(exc)})
        except StoreError as exc:
            return self._json(404, {"error": "store_error", "detail": str(exc)})
        except (KeyError, ValueError) as exc:
            key = exc.args[0] if isinstance(exc, KeyError) and exc.args else str(exc)
            return self._json(400, {"error": "bad_request", "detail": f"缺少或非法参数: {key}"})

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def log_message(self, format: str, *args: object) -> None:
        return


def create_server(host: str, port: int, service: Service | None = None) -> ThreadingHTTPServer:
    handler = type("BoundRequestHandler", (RequestHandler,),
                   {"service": service or build_service()})
    return ThreadingHTTPServer((host, port), handler)
