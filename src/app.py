"""HTTP 接口层：标准库实现，JSON 请求/响应，统一错误格式。"""
from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from engine import RebuildError
from service import Service, ServiceError, ValidationError
from store import Store

SERVICE_NAME = '公司行动核算服务'


def health_payload() -> dict[str, str]:
    return {"status": "ok", "service": SERVICE_NAME}


def _query_date(query: dict) -> str:
    values = query.get("date")
    if not values or not values[0]:
        raise ValidationError("缺少查询参数 date")
    return values[0]


def _query_version(query: dict) -> int | None:
    values = query.get("version")
    if values and values[0]:
        return int(values[0])
    return None


def build_routes():
    """路由表：(方法, 路径正则, 处理函数)。处理函数返回 (status, payload)。"""
    def h_health(service, match, query, body):
        return 200, health_payload()

    def h_ingest(service, match, query, body):
        response, status = service.ingest(body or {})
        return status, response

    def h_recalculate(service, match, query, body):
        return 200, service.recalculate((body or {}).get("trigger_message_ids"))

    def h_versions(service, match, query, body):
        return 200, service.list_versions()

    def h_version(service, match, query, body):
        return 200, service.get_version(int(match.group(1)))

    def h_positions(service, match, query, body):
        return 200, service.positions(match.group(1), _query_date(query),
                                      _query_version(query))

    def h_cash(service, match, query, body):
        return 200, service.cash(match.group(1), _query_date(query),
                                 _query_version(query))

    def h_pending(service, match, query, body):
        return 200, service.pending(match.group(1), _query_date(query),
                                    _query_version(query))

    def h_realized(service, match, query, body):
        return 200, service.realized(match.group(1), _query_version(query))

    def h_lineage(service, match, query, body):
        security = (query.get("security_id") or [None])[0]
        if not security:
            raise ValidationError("缺少查询参数 security_id")
        return 200, service.lineage(match.group(1), security, _query_date(query),
                                    _query_version(query))

    def h_lineage_export(service, match, query, body):
        security = (query.get("security_id") or [None])[0]
        if not security:
            raise ValidationError("缺少查询参数 security_id")
        return 200, service.export_lineage(match.group(1), security, _query_date(query),
                                           _query_version(query))

    def h_lineage_verify(service, match, query, body):
        return 200, service.verify_lineage(body or {})

    def h_snapshot_create(service, match, query, body):
        body = body or {}
        account_id = body.get("account_id")
        day = body.get("date")
        if not account_id or not day:
            raise ValidationError("快照需要 account_id 与 date")
        return 201, service.issue_snapshot(account_id, day, body.get("label", ""))

    def h_snapshots(service, match, query, body):
        return 200, service.list_snapshots()

    def h_snapshot(service, match, query, body):
        return 200, service.get_snapshot(match.group(1))

    def h_snapshot_compare(service, match, query, body):
        return 200, service.compare_snapshot(match.group(1))

    def h_actions(service, match, query, body):
        return 200, service.corporate_actions(_query_version(query))

    def h_fx(service, match, query, body):
        base = (query.get("base") or [None])[0]
        quote = (query.get("quote") or [None])[0]
        day = _query_date(query)
        if not base or not quote:
            raise ValidationError("缺少查询参数 base/quote")
        return 200, service.fx_lookup(base, quote, day)

    def h_events(service, match, query, body):
        kind = (query.get("kind") or [None])[0]
        limit = int((query.get("limit") or ["200"])[0])
        return 200, service.list_events(kind, limit)

    return [
        ("GET", re.compile(r"^/health$"), h_health),
        ("POST", re.compile(r"^/v1/messages$"), h_ingest),
        ("POST", re.compile(r"^/v1/recalculate$"), h_recalculate),
        ("GET", re.compile(r"^/v1/versions$"), h_versions),
        ("GET", re.compile(r"^/v1/versions/(\d+)$"), h_version),
        ("GET", re.compile(r"^/v1/accounts/([^/]+)/positions$"), h_positions),
        ("GET", re.compile(r"^/v1/accounts/([^/]+)/cash$"), h_cash),
        ("GET", re.compile(r"^/v1/accounts/([^/]+)/pending$"), h_pending),
        ("GET", re.compile(r"^/v1/accounts/([^/]+)/realized$"), h_realized),
        ("GET", re.compile(r"^/v1/accounts/([^/]+)/lineage$"), h_lineage),
        ("GET", re.compile(r"^/v1/accounts/([^/]+)/lineage/export$"), h_lineage_export),
        ("POST", re.compile(r"^/v1/lineage/verify$"), h_lineage_verify),
        ("POST", re.compile(r"^/v1/snapshots$"), h_snapshot_create),
        ("GET", re.compile(r"^/v1/snapshots$"), h_snapshots),
        ("GET", re.compile(r"^/v1/snapshots/([^/]+)$"), h_snapshot),
        ("GET", re.compile(r"^/v1/snapshots/([^/]+)/compare$"), h_snapshot_compare),
        ("GET", re.compile(r"^/v1/corporate-actions$"), h_actions),
        ("GET", re.compile(r"^/v1/fx-rates/lookup$"), h_fx),
        ("GET", re.compile(r"^/v1/events$"), h_events),
    ]


class RequestHandler(BaseHTTPRequestHandler):
    service: Service = None  # 由 create_server 注入
    routes = build_routes()

    def _handle(self, method: str) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        body = None
        if method == "POST":
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            if raw:
                try:
                    body = json.loads(raw.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    self._respond(400, {"error": {"code": "bad_json",
                                                  "message": "请求体不是合法 JSON"}})
                    return
        for route_method, pattern, handler in self.routes:
            if route_method != method:
                continue
            match = pattern.match(parsed.path)
            if not match:
                continue
            try:
                status, payload = handler(self.service, match, query, body)
            except RebuildError as exc:
                status, payload = 422, {"error": {"code": "rebuild_failed",
                                                  "message": exc.message,
                                                  "details": exc.details}}
            except ServiceError as exc:
                status, payload = exc.status, {"error": {"code": exc.code,
                                                         "message": exc.message,
                                                         "details": exc.details}}
            except Exception as exc:  # noqa: BLE001 - 兜底，保证 JSON 错误格式
                status, payload = 500, {"error": {"code": "internal_error",
                                                  "message": str(exc)}}
            self._respond(status, payload)
            return
        self._respond(404, {"error": {"code": "not_found", "message": "接口不存在"}})

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def _respond(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


def create_server(host: str, port: int, service: Service | None = None) -> ThreadingHTTPServer:
    if service is None:
        service = Service(Store(".runtime"))
    RequestHandler.service = service
    return ThreadingHTTPServer((host, port), RequestHandler)
