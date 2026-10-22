"""Loopback HTTP service. Deploy behind an owner-managed HTTPS reverse proxy."""
import html
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import re
import threading
from urllib.parse import parse_qs, urlsplit

from .. import __version__
from . import AUTHORITY
from .adapters import Dispatcher, Events, encode, post_https
from .auth import OAuth
from .config import Rejected, digest
from .protocol import Protocol, TOOLS


class App:
    def __init__(self, store, post=post_https):
        self.store = store
        self.oauth = OAuth(store)
        self.events = Events(store, post)
        self.protocol = Protocol(store, self.events)
        self.dispatcher = Dispatcher(store, post)
        self.rates = {}
        self.rate_lock = threading.Lock()
        self.worker_warning = False

    def limit(self, source, action):
        with self.rate_lock:
            now = self.store.clock()
            self.rates = {k: v for k, v in self.rates.items() if now - v[0] < 60}
            key = (source, action)
            started, count = self.rates.get(key, (now, 0))
            ceiling = 10 if action == "/oauth/authorize" else 60
            if count >= ceiling or len(self.rates) > 10000:
                raise Rejected("Request limit reached; wait before trying again")
            self.rates[key] = (started, count + 1)

    def rpc(self, actor, request):
        mid = request.get("id")
        if mid is None:
            return None
        method, params = request.get("method"), request.get("params", {})
        try:
            if not isinstance(params, dict):
                raise Rejected("params must be an object")
            if method == "initialize":
                result = {"protocolVersion": params.get("protocolVersion", "2026-07-28"), "capabilities": {"tools": {}, "events": {}},
                          "serverInfo": {"name": "AgentPeers personal agents", "version": __version__}, "instructions": AUTHORITY + " Discover with list_peers. On each event claim_message once, stop if not claimed, consume and acknowledge, then reply only when expects=reply and within the human's scope. Use stable IDs. Never poll on a schedule or auto-ack unread content."}
            elif method == "server/discover":
                result = {"resultType": "complete", "supportedVersions": ["2026-07-28"], "capabilities": {"tools": {}, "events": {}}}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                try:
                    value = self.protocol.call(actor, params.get("name"), params.get("arguments", {}))
                    result = {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}], "structuredContent": value}
                except Rejected as exc:
                    result = {"isError": True, "content": [{"type": "text", "text": str(exc)}]}
            elif method == "events/list":
                result = self.protocol.event_catalog(actor)
            elif method == "events/subscribe":
                result = self.events.subscribe(actor, params)
            elif method == "events/unsubscribe":
                result = self.events.unsubscribe(actor, params)
            else:
                return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "Method not found"}}
            return {"jsonrpc": "2.0", "id": mid, "result": result}
        except Rejected as exc:
            code = -32015 if method == "events/subscribe" and "verification failed" in str(exc) else -32602
            error = {"code": code, "message": str(exc)}
            if code == -32015:
                error["data"] = {"reason": "challenge_failed"}
            return {"jsonrpc": "2.0", "id": mid, "error": error}

    def callback(self, mid, token, args):
        with self.store.lock:
            row = self.store._message(mid)
            if not row["callback_hash"] or not hmac.compare_digest(digest(token), row["callback_hash"]):
                raise Rejected("Unauthorized callback")
            actor = row["recipient"]
        action = args.get("action")
        if action == "claim":
            return self.store.claim(actor, mid)
        if action == "acknowledge":
            return self.store.ack(actor, mid)
        if action == "status":
            return self.store.get(actor, mid)
        if action == "reply":
            return self.protocol.call(actor, "reply", {"message_id": mid, "message": args.get("message"), "reply_id": args.get("reply_id"), "expects": args.get("expects", "none")})
        raise Rejected("Allowed callback actions: claim, acknowledge, reply, status")


def form(raw):
    fields = parse_qs(raw.decode("utf-8"), keep_blank_values=True, max_num_fields=30)
    if any(len(values) != 1 for values in fields.values()):
        raise Rejected("Repeated form fields are forbidden")
    return {key: values[0] for key, values in fields.items()}


def handler(app):
    class Handler(BaseHTTPRequestHandler):
        server_version = "AgentPeers"
        sys_version = ""
        request_timeout = 10

        def setup(self):
            super().setup()
            # Bound idle and partial-header connections before parsing any request.
            self.connection.settimeout(self.request_timeout)

        def log_message(self, *_args):
            pass  # Callback paths, authorization codes and query strings are credentials.

        def output(self, status, value=None, headers=None, content_type="application/json"):
            body = b"" if value is None else encode(value) if content_type == "application/json" else value.encode()
            self.send_response(status)
            for key, value in {"Content-Type": content_type, "Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff", "Content-Length": str(len(body)), **(headers or {})}.items():
                self.send_header(key, value)
            self.end_headers()
            if body:
                self.wfile.write(body)

        def auth(self, resource=None):
            authorization = self.headers.get("Authorization", "")
            if not authorization.startswith("Bearer "):
                raise Rejected("Unauthorized")
            return app.oauth.actor(authorization[7:], resource)

        def read_body(self):
            if self.headers.get("Transfer-Encoding"):
                raise Rejected("Transfer-Encoding is not supported")
            try:
                length = int(self.headers.get("Content-Length", "-1"))
            except ValueError:
                raise Rejected("Content-Length required") from None
            if not 0 <= length <= 262144:
                raise Rejected("Request body must be at most 256 KiB")
            self.connection.settimeout(10)
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise Rejected("Incomplete request body")
            if self.headers.get("Content-Type", "").split(";")[0] == "application/x-www-form-urlencoded":
                return form(raw)
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise Rejected("Request must be an object")
            return value

        def do_GET(self):
            try:
                parsed = urlsplit(self.path)
                path = parsed.path
                base = app.store.config["public_url"]
                if path == "/health":
                    return self.output(503 if app.worker_warning else 200, {"service": "AgentPeers", "delivery_verified": False, "worker_warning": app.worker_warning})
                if path == "/.well-known/oauth-authorization-server":
                    return self.output(200, app.oauth.metadata())
                prefix = "/.well-known/oauth-protected-resource/mcp/"
                if path.startswith(prefix):
                    resource = base + "/mcp/" + path[len(prefix):]
                    app.oauth.resource_peer(resource)
                    return self.output(200, {"resource": resource, "authorization_servers": [base], "scopes_supported": ["agentpeers:messages"]})
                if path == "/oauth/authorize":
                    app.limit(self.client_address[0], path)
                    args = form(parsed.query.encode())
                    app.oauth.validate_authorize(args)
                    peer = app.oauth.resource_peer(args["resource"])
                    fields = {**args, "csrf": app.oauth.csrf(args)}
                    hidden = "".join('<input type="hidden" name="%s" value="%s">' % (html.escape(k, quote=True), html.escape(v, quote=True)) for k, v in fields.items())
                    body = '<!doctype html><meta charset="utf-8"><title>Connect AgentPeers</title><h1>Connect %s to AgentPeers</h1><p>This client will be able to read and send this peer\'s messages to its owner-connected peers. It cannot manage connections or run commands.</p><form method="post" action="/oauth/authorize">%s<label>Owner key <input name="owner_key" type="password" autocomplete="off" required></label><button type="submit">Allow messaging</button></form>' % (html.escape(app.store.config["peers"][peer]["name"]), hidden)
                    return self.output(200, body, {"Content-Security-Policy": "default-src 'none'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"}, "text/html; charset=utf-8")
                self.output(404, {"error": "Not found"})
            except Rejected as exc:
                self.output(400, {"error": str(exc)})
            except Exception:
                self.output(400, {"error": "Invalid request"})

        def do_POST(self):
            path = urlsplit(self.path).path
            resource = None
            try:
                if path.startswith("/oauth/"):
                    app.limit(self.client_address[0], path)
                    args = self.read_body()
                    if path == "/oauth/register":
                        return self.output(201, app.oauth.register(args))
                    if path == "/oauth/token":
                        return self.output(200, app.oauth.token(args))
                    if path == "/oauth/revoke":
                        return self.output(200, app.oauth.revoke(args))
                    if path == "/oauth/authorize":
                        if self.headers.get("Origin") != app.store.config["public_url"]:
                            raise Rejected("Owner authorization requires the same HTTPS origin")
                        destination = app.oauth.authorize(args)
                        return self.output(303, headers={"Location": destination})
                if path.startswith("/mcp/"):
                    resource = app.store.config["public_url"] + path
                    actor = self.auth(resource)
                    request = self.read_body()
                    result = app.rpc(actor, request)
                    return self.output(200 if result is not None else 202, result)
                if path == "/v1/call":
                    actor = self.auth()
                    args = self.read_body()
                    return self.output(200, app.protocol.call(actor, args.get("tool"), args.get("arguments", {})))
                match = re.fullmatch(r"/callbacks/([A-Za-z0-9._:-]{1,100})/([A-Za-z0-9_-]{40,100})", path)
                if match:
                    return self.output(200, app.callback(*match.groups(), self.read_body()))
                self.output(404, {"error": "Not found"})
            except Rejected as exc:
                unauthorized = str(exc) in ("Unauthorized", "Unauthorized callback")
                headers = {}
                if unauthorized and resource:
                    metadata = app.store.config["public_url"] + "/.well-known/oauth-protected-resource" + path
                    headers["WWW-Authenticate"] = 'Bearer resource_metadata="%s", scope="agentpeers:messages"' % metadata
                self.output(401 if unauthorized else 400, {"error": str(exc)}, headers)
            except Exception:
                self.output(400, {"error": "Invalid request"})
    return Handler


def serve(app, host="127.0.0.1", port=8766):
    if host != "127.0.0.1":
        raise Rejected("Bind only to loopback; use an HTTPS reverse proxy")
    server = ThreadingHTTPServer((host, port), handler(app))
    done = threading.Event()
    def deliver():
        while not done.is_set():
            try:
                worked = app.dispatcher.once()
                app.worker_warning = False
            except Exception:
                # Leave the durable row for inspection; don't expose a traceback with credentials.
                worked = False
                app.worker_warning = True
            done.wait(0.1 if worked else 1)
    worker = threading.Thread(target=deliver, daemon=True)
    worker.start()
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        done.set()
        server.server_close()
        worker.join(timeout=12)
