"""Exercise real HTTP framing and bounded connections using isolated loopback sockets."""
from contextlib import contextmanager
import http.client
import json
from pathlib import Path
import queue
import socket
import tempfile
import threading
import time
import unittest
from urllib.parse import urlencode

import start_stop_service as service


class Workspace:
    collection_config = None

    def __init__(self, root):
        self.scratch_dir = root
        self.jobs = []
        self.uploads = []

    def start_job(self, action, **kwargs):
        self.jobs.append(action)
        return {"id": "isolated-test", "status": "queued"}

    def upload_file(self, path, **kwargs):
        self.uploads.append(Path(path).read_bytes())
        return {"upload_id": "isolated-upload"}


@contextmanager
def running_server(*, lan=False, auth=False, connections=4, header_timeout=1.0, body_timeout=1.0):
    with tempfile.TemporaryDirectory() as temporary:
        workspace = Workspace(Path(temporary))
        host = "192.168.1.20" if lan else "127.0.0.1"
        handler = service.create_handler(
            workspace, lan_read_only=lan, lan_no_auth=lan and not auth,
            lan_auth_credentials=service.BasicAuthCredentials("reader", "test-password") if auth else None,
            public_host=host,
        )
        handler.timeout = 1.0
        handler.header_timeout = header_timeout
        handler.json_body_timeout = body_timeout
        handler.upload_body_timeout = body_timeout
        handler.log_message = lambda *_args: None
        admitted = queue.Queue()
        setup = handler.setup

        def record_setup(self):
            setup(self)
            admitted.put(True)

        handler.setup = record_setup
        server_type = type("TestHTTPServer", (service.StartStopHTTPServer,), {"max_connections": connections})
        server = server_type(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        try:
            yield server, workspace, host, admitted
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)


def wire_request(method, path, authority, body=b"", headers=()):
    lines = [f"{method} {path} HTTP/1.1", f"Host: {authority}", f"Content-Length: {len(body)}"]
    lines.extend(f"{name}: {value}" for name, value in headers)
    return ("\r\n".join(lines) + "\r\n\r\n").encode() + body


def response(sock):
    result = http.client.HTTPResponse(sock)
    result.begin()
    body = result.read()
    return result.status, result.getheader("Connection"), body


@contextmanager
def trickling(sock, chunk):
    stop = threading.Event()

    def send():
        while not stop.wait(0.02):
            try:
                sock.sendall(chunk)
            except OSError:
                return

    sender = threading.Thread(target=send, daemon=True)
    sender.start()
    try:
        yield
    finally:
        stop.set()
        sender.join(1)


class HTTPConnectionTests(unittest.TestCase):
    def assert_closed(self, sock):
        try:
            self.assertEqual(sock.recv(1), b"")
        except ConnectionResetError:
            pass

    def test_rejected_outer_requests_never_execute_embedded_mutation(self):
        cases = [
            ("POST", "/api/start-stop/jobs", [("Content-Type", "text/plain"), ("Origin", "http://example.invalid")], 415),
            ("POST", "/api/start-stop/jobs", [("Content-Type", "application/json"), ("Origin", "http://example.invalid")], 403),
            ("POST", "/unknown", [], 404),
            ("PUT", "/unknown", [], 404),
            ("POST", "/api/start-stop/uploads?filename=bad", [("Content-Type", "application/octet-stream")], 400),
            ("GET", "/healthz", [], 200),
        ]
        with running_server() as (server, workspace, host, _):
            authority = f"{host}:{server.server_port}"
            inner = wire_request("POST", "/api/start-stop/jobs", authority, b'{"action":"scan"}', [("Content-Type", "application/json")])
            for method, path, headers, expected in cases:
                with self.subTest(method=method, path=path, headers=headers), socket.create_connection(server.server_address, timeout=2) as sock:
                    sock.sendall(wire_request(method, path, authority, inner, headers))
                    status, connection, _ = response(sock)
                    self.assertEqual(status, expected)
                    self.assertEqual(connection, "close")
                    self.assert_closed(sock)
                    self.assertEqual(workspace.jobs, [])
                    self.assertEqual(workspace.uploads, [])

    def test_auth_and_read_only_rejections_close_without_consuming_body(self):
        for auth, expected in [(True, 401), (False, 403)]:
            with self.subTest(auth=auth), running_server(lan=True, auth=auth) as (server, workspace, host, _):
                authority = f"{host}:{server.server_port}"
                with socket.create_connection(server.server_address, timeout=2) as sock:
                    # Declare a body but never send it: rejection must be immediate.
                    sock.sendall(f"POST /api/start-stop/jobs HTTP/1.1\r\nHost: {authority}\r\nContent-Length: 500\r\n\r\n".encode())
                    status, connection, _ = response(sock)
                    self.assertEqual((status, connection), (expected, "close"))
                    self.assert_closed(sock)
                    self.assertEqual(workspace.jobs, [])

    def test_valid_json_and_upload_keep_connection_usable(self):
        with running_server() as (server, workspace, _, _):
            client = http.client.HTTPConnection(*server.server_address, timeout=2)
            try:
                client.request("POST", "/api/start-stop/jobs", b'{"action":"scan"}', {"Content-Type": "application/json"})
                result = client.getresponse()
                self.assertEqual(result.status, 202)
                self.assertNotEqual(result.getheader("Connection"), "close")
                result.read()
                connection = client.sock
                content = b"synthetic upload contents\n"
                query = urlencode({"filename": "sample.txt", "relative_path": "sample.txt", "group": "audit", "last_modified": "1000", "size": str(len(content))})
                client.request("POST", "/api/start-stop/uploads?" + query, content, {"Content-Type": "application/octet-stream"})
                result = client.getresponse()
                self.assertEqual(result.status, 201, result.read())
                result.read()
                client.request("GET", "/healthz")
                result = client.getresponse()
                self.assertEqual(json.loads(result.read()), {"ok": True})
                self.assertIs(client.sock, connection)
                self.assertEqual(workspace.jobs, ["scan"])
                self.assertEqual(workspace.uploads, [content])
            finally:
                client.close()

    def test_short_json_body_never_mutates(self):
        with running_server() as (server, workspace, host, _), socket.create_connection(server.server_address, timeout=2) as sock:
            authority = f"{host}:{server.server_port}"
            sock.sendall(f'POST /api/start-stop/jobs HTTP/1.1\r\nHost: {authority}\r\nContent-Type: application/json\r\nContent-Length: 100\r\n\r\n{{"action":"scan"}}'.encode())
            sock.shutdown(socket.SHUT_WR)
            self.assertEqual(response(sock)[:2], (400, "close"))
            self.assertEqual(workspace.jobs, [])

    def test_header_deadline_expires_even_with_continuous_trickle(self):
        with running_server(header_timeout=0.15) as (server, _, _, admitted), socket.create_connection(server.server_address, timeout=2) as sock:
            admitted.get(timeout=1)
            started = time.monotonic()
            with trickling(sock, b"G"):
                self.assert_closed(sock)
                self.assertLess(time.monotonic() - started, 1.0)

    def test_json_and_upload_body_deadlines_prevent_partial_writes(self):
        for upload in [False, True]:
            with self.subTest(upload=upload), running_server(body_timeout=0.15) as (server, workspace, host, _), socket.create_connection(server.server_address, timeout=2) as sock:
                path = "/api/start-stop/jobs"
                media_type = "application/json"
                if upload:
                    path = "/api/start-stop/uploads?" + urlencode({"filename": "sample.txt", "relative_path": "sample.txt", "group": "audit", "last_modified": "1000", "size": "100"})
                    media_type = "application/octet-stream"
                sock.sendall(f"POST {path} HTTP/1.1\r\nHost: {host}:{server.server_port}\r\nContent-Type: {media_type}\r\nContent-Length: 100\r\n\r\n{{".encode())
                with trickling(sock, b" "):
                    status, connection, _ = response(sock)
                self.assertEqual((status, connection), (408, "close"))
                self.assert_closed(sock)
                self.assertEqual(workspace.jobs, [])
                self.assertEqual(workspace.uploads, [])
                self.assertEqual(list(workspace.scratch_dir.iterdir()), [])

    def test_upload_can_stream_longer_than_header_deadline(self):
        with running_server(header_timeout=0.1, body_timeout=1.0) as (server, workspace, host, _), socket.create_connection(server.server_address, timeout=2) as sock:
            content = b"synthetic streamed upload\n"
            query = urlencode({"filename": "sample.txt", "relative_path": "sample.txt", "group": "audit", "last_modified": "1000", "size": str(len(content))})
            request = wire_request("POST", "/api/start-stop/uploads?" + query, f"{host}:{server.server_port}", content, [("Content-Type", "application/octet-stream")])
            sock.sendall(request[:-len(content)])
            for index in range(0, len(content), 4):
                sock.sendall(content[index:index + 4])
                time.sleep(0.025)
            self.assertEqual(response(sock)[0], 201)
            self.assertEqual(workspace.uploads, [content])

    def test_connection_limit_rejects_excess_and_recovers_after_idle_timeout(self):
        with running_server(connections=2, header_timeout=0.25) as (server, _, _, admitted):
            with socket.create_connection(server.server_address, timeout=2) as first, socket.create_connection(server.server_address, timeout=2) as second:
                admitted.get(timeout=1)
                admitted.get(timeout=1)
                with socket.create_connection(server.server_address, timeout=2) as excess:
                    self.assert_closed(excess)
                self.assert_closed(first)
                self.assert_closed(second)
            # Both idle handlers have ended; accepting and serving resumes.
            client = http.client.HTTPConnection(*server.server_address, timeout=2)
            try:
                client.request("GET", "/healthz")
                result = client.getresponse()
                self.assertEqual((result.status, json.loads(result.read())), (200, {"ok": True}))
            finally:
                client.close()

    def test_completed_keep_alive_connection_expires_when_idle(self):
        with running_server(header_timeout=0.15) as (server, _, host, _), socket.create_connection(server.server_address, timeout=2) as sock:
            sock.sendall(wire_request("GET", "/healthz", f"{host}:{server.server_port}"))
            self.assertEqual(response(sock)[0], 200)
            self.assert_closed(sock)


if __name__ == "__main__":
    unittest.main()
