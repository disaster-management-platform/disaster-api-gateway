"""Gateway checks with real APIs, temporary PostgreSQL databases and Node.js for shared frontend requests."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, closing, contextmanager
import psycopg2
from psycopg2 import sql
import hashlib
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import tempfile
import time
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
GATEWAY = ROOT / "DisasterLocation.Gateway"
BACKEND = Path(os.environ.get("BACKEND_PROJECT_PATH", ROOT.parent / "disaster-backend-offline/DisasterLocation"))
RECONSTRUCTION = Path(os.environ.get("RECONSTRUCTION_REPOSITORY_PATH", ROOT.parent / "reconstruction-business-layer"))


@contextmanager
def test_database():
    settings = psycopg2.extensions.parse_dsn(os.environ["TEST_POSTGRES_DSN"])
    if not settings.get("host", "").startswith("/"):
        raise ValueError("Las pruebas requieren un socket PostgreSQL local, nunca Supabase.")
    name = "gateway_test_" + uuid.uuid4().hex
    with closing(psycopg2.connect(**settings)) as admin:
        admin.autocommit = True
        with admin.cursor() as cursor:
            cursor.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        try:
            settings["dbname"] = name
            def escape(value):
                return '"' + value.replace('"', '""') + '"'
            yield ";".join(key + "=" + escape(settings[source]) for key, source in
                [("Host", "host"), ("Port", "port"), ("Database", "dbname"), ("Username", "user"), ("Password", "password")]
                if source in settings)
        finally:
            with admin.cursor() as cursor:
                cursor.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def request(port, path="/api/desastres", method="GET", body=None, headers=None):
    values = dict(headers or {})
    if isinstance(body, dict):
        body = json.dumps(body).encode()
        values.setdefault("Content-Type", "application/json")
    connection = HTTPConnection("127.0.0.1", port, timeout=8)
    try:
        connection.request(method, path, body=body, headers=values)
        response = connection.getresponse()
        data = response.read()
        result = json.loads(data) if data and "json" in response.getheader("Content-Type", "") else data
        return response.status, dict((k.lower(), v) for k, v in response.getheaders()), result
    finally:
        connection.close()


def wait_ready(port, path, process):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError("Process exited during startup")
        try:
            if request(port, path)[0] == 200:
                return
        except (OSError, TimeoutError):
            pass
        time.sleep(.1)
    raise AssertionError("Process did not become ready")


class GatewayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="gateway-tests-")
        cls.folder = Path(cls.temp.name)
        cls.databases = ExitStack()
        cls.processes = []
        cls.logs = []
        cls.addClassCleanup(cls.cleanup)
        connection = cls.databases.enter_context(test_database())
        with tempfile.TemporaryFile(mode="w+") as log:
            result = subprocess.run(["dotnet", str(BACKEND / "bin/Debug/net10.0/DisasterLocation.dll"),
                    "--Database:Migrate=true"], cwd=BACKEND,
                    env={**os.environ, "ConnectionStrings__Disasters": connection}, stdout=log, stderr=log, timeout=60)
            if result.returncode:
                log.seek(0)
                raise AssertionError(log.read()[-6000:])
        cls.disaster_api, cls.original_process, _ = cls.start(BACKEND, {
            "ConnectionStrings__Disasters": connection, "Security__LocalDemo": "true"}, "/api/desastres")
        cls.peer_api, _, _ = cls.start(BACKEND, {
            "ConnectionStrings__Disasters": connection, "Security__LocalDemo": "true"}, "/api/desastres")
        common = {"ConnectionStrings__Disasters": connection,
                  "Security__LocalDemo": "true", "Messaging__Enabled": "false",
                  "Reconstruction__DemoExternalData": "true", "Attachments__Directory": str(cls.folder / "attachments")}
        common["ExternalApis__Disasters"] = f"http://127.0.0.1:{cls.disaster_api}/"
        cls.reconstruction_api, _, _ = cls.start(RECONSTRUCTION / "Reconstruction.Api", common, "/api/reconstruccion/health")
        cls.secure_api, cls.secure_process, _ = cls.start(RECONSTRUCTION / "Reconstruction.Api",
            {**common, "Security__LocalDemo": "false"}, "/api/reconstruccion/health")
        cls.original, _, _ = cls.gateway()
        cls.expanded, _, _ = cls.gateway("Reconstruction")

    @classmethod
    def cleanup(cls):
        for process in reversed(cls.processes):
            if process.poll() is None:
                process.send_signal(signal.SIGCONT)
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        for log in cls.logs:
            log.close()
        cls.databases.close()
        cls.temp.cleanup()

    @classmethod
    def start(cls, folder, env, health):
        port = free_port()
        log_path = cls.folder / f"process-{len(cls.processes)}.log"
        log = open(log_path, "w+")
        cls.logs.append(log)
        process = subprocess.Popen(["dotnet", str(folder / "bin/Debug/net10.0" / (folder.name + ".dll")),
            "--urls", f"http://127.0.0.1:{port}"], cwd=folder,
            env={**os.environ, "ASPNETCORE_ENVIRONMENT": "Development", **env}, stdout=log, stderr=log)
        cls.processes.append(process)
        try:
            wait_ready(port, health, process)
        except Exception:
            log.flush()
            print(log_path.read_text()[-6000:])
            raise
        return port, process, log_path

    @classmethod
    def gateway(cls, context="Original", overrides=None):
        env = {"ASPNETCORE_ENVIRONMENT": "Reconstruction" if context == "Reconstruction" else "Development",
               "ReverseProxy__Clusters__disasters-api__Destinations__local__Address":
                   f"http://127.0.0.1:{cls.disaster_api}/",
               "ReverseProxy__Clusters__reconstruction-api__Destinations__local__Address":
                   f"http://127.0.0.1:{cls.reconstruction_api}/"}
        if context == "Original":
            del env["ReverseProxy__Clusters__reconstruction-api__Destinations__local__Address"]
        return cls.start(GATEWAY, {**env, **(overrides or {})}, "/health")

    @staticmethod
    def report(name=None):
        return {"nombre": name or str(uuid.uuid4()), "ubicacion": "Bogotá", "severidad": 4,
                "descripcion": "Gateway validation", "latitud": 4.7, "longitud": -74.0}

    def test_contexts_share_contracts_frontends_and_disasters(self):
        original_body = self.report()
        expanded_body = {**self.report(), "radio": 500}
        status, _, original = request(self.original, method="POST", body=original_body)
        self.assertEqual(201, status)
        self.assertEqual(set(original_body) | {"id", "radio", "phase", "version"}, set(original))
        self.assertIsNone(original["radio"])
        status, _, expanded = request(self.expanded, method="POST", body=expanded_body)
        self.assertEqual(201, status)
        self.assertEqual(set(expanded_body) | {"id", "phase", "version"}, set(expanded))
        self.assertIn(expanded_body["nombre"], [d["nombre"] for d in request(self.original)[2]])
        self.assertIn(original_body["nombre"], [d["nombre"] for d in request(self.expanded)[2]])
        self.assertEqual(original, request(self.expanded, f"/api/desastres/{original['id']}")[2])
        self.assertEqual(expanded, request(self.original, f"/api/desastres/{expanded['id']}")[2])
        self.assertEqual(200, request(self.original, f"/api/desastres/{original['id']}/fase", "PUT",
                                     {"phase": "Response", "expectedVersion": 1})[0])
        self.assertEqual(200, request(self.expanded, f"/api/desastres/{expanded['id']}/fase", "PUT",
                                     {"phase": "Response", "expectedVersion": expanded["version"]})[0])
        key = {"Idempotency-Key": str(uuid.uuid4())}
        status, _, shared = request(self.original, method="POST", body=self.report("shared-" + str(uuid.uuid4())), headers=key)
        self.assertEqual(201, status)
        shared_body = {field: shared[field] for field in original_body}
        status, _, repeated = request(self.expanded, method="POST", body={**shared_body, "radio": None}, headers=key)
        self.assertEqual(200, status)
        self.assertEqual(shared, repeated)
        self.assertEqual(409, request(self.expanded, method="POST", body={**shared_body, "radio": 500}, headers=key)[0])
        self.assertEqual(shared, request(self.original, f"/api/desastres/{shared['id']}")[2])
        for port, context in [(self.original, "Original"), (self.expanded, "Reconstruction")]:
            for filename in ["index.html"]:
                self.assertEqual((GATEWAY / "Frontends" / context / filename).read_bytes(), request(port, "/" + filename)[2])
            self.assertEqual(request(port, "/index.html")[2], request(port, "/")[2])
        for filename in ["styles.css", "app.js"]:
            self.assertEqual((GATEWAY / "wwwroot" / filename).read_bytes(), request(self.original, "/" + filename)[2])
            self.assertEqual(request(self.original, "/" + filename)[2], request(self.expanded, "/" + filename)[2])
        self.assertEqual(404, request(self.original, "/session.js")[0])
        for asset in (GATEWAY / "Frontends/Reconstruction").iterdir():
            self.assertEqual(asset.read_bytes(), request(self.expanded, "/" + asset.name)[2])

    def test_shared_frontend_preserves_each_authentication_behavior(self):
        script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('wwwroot/app.js', 'utf8');
const api = source.slice(source.indexOf('async function api('), source.indexOf('\nfunction textElement'));
(async () => {
  for (const [port, authenticated] of JSON.parse(process.env.FRONTEND_CONTEXTS)) {
    const calls = [];
    const fetch = (path, options = {}) => {
      calls.push({path, headers: new Headers(options.headers)});
      return globalThis.fetch(`http://127.0.0.1:${port}${path}`, options);
    };
    const context = vm.createContext({fetch, Headers, window: {fetch, addEventListener() {}},
      sessionStorage: {getItem() { return 'frontend-header-test'; }}});
    if (authenticated) vm.runInContext(fs.readFileSync('Frontends/Reconstruction/session.js', 'utf8'), context);
    vm.runInContext(api, context);
    await vm.runInContext("api('')", context);
    assert.equal(calls[0].path, '/api/desastres');
    assert.equal(calls[0].headers.get('Authorization'), authenticated ? 'Bearer frontend-header-test' : null);
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
        subprocess.run(["node", "-"], input=script, text=True, cwd=GATEWAY, check=True,
            env={**os.environ, "FRONTEND_CONTEXTS": json.dumps([(self.original, False), (self.expanded, True)])})

    def test_custom_destination_names_do_not_keep_implicit_local_destinations(self):
        for context, api in [("Original", self.disaster_api), ("Reconstruction", self.disaster_api)]:
            with self.subTest(context=context):
                body = self.report()
                if context == "Reconstruction": body["radio"] = 500
                self.assertEqual(201, request(api, method="POST", body=body)[0])
                expected = request(api)[2]
                env = {"ASPNETCORE_ENVIRONMENT": "Reconstruction" if context == "Reconstruction" else "Development",
                       "ReverseProxy__Clusters__disasters-api__Destinations__external__Address": f"http://127.0.0.1:{api}/"}
                if context == "Reconstruction":
                    env["ReverseProxy__Clusters__reconstruction-api__Destinations__external__Address"] = f"http://127.0.0.1:{self.reconstruction_api}/"
                port, _, log = self.start(GATEWAY, env, "/health")
                for _ in range(4):
                    status, _, data = request(port)
                    self.assertEqual(200, status)
                    self.assertEqual(expected, data)
                    if context == "Reconstruction":
                        status, _, projects = request(port, "/api/reconstruccion/proyectos")
                        self.assertEqual(200, status)
                        self.assertEqual(request(self.reconstruction_api, "/api/reconstruccion/proyectos")[2], projects)
                self.assertNotIn("via local", log.read_text())

    def test_missing_destinations_do_not_fall_back_to_local_apis(self):
        for environment in ["Development", "Reconstruction"]:
            with self.subTest(environment=environment):
                port, _, _ = self.start(GATEWAY, {"ASPNETCORE_ENVIRONMENT": environment}, "/health")
                self.assertEqual(503, request(port)[0])
                if environment == "Reconstruction":
                    self.assertEqual(503, request(port, "/api/reconstruccion/proyectos")[0])

    def test_routes_methods_queries_and_cors(self):
        port, _, _ = self.gateway("Reconstruction", {"Cors__AllowedOrigins__0": "http://localhost:3000"})
        self.assertEqual(200, request(port, "/api/reconstruccion/proyectos?disasterId=999999")[0])
        self.assertEqual([], request(port, "/api/reconstruccion/proyectos?disasterId=999999")[2])
        for path in ["/api/internal", "/api/desastres/internal", "/api/reconstruccion/internal", "/appsettings.json",
                     "/Frontends/Original/index.html"]:
            self.assertEqual(404, request(port, path)[0], path)
        self.assertEqual(405, request(port, method="DELETE")[0])
        status, headers, _ = request(port, method="OPTIONS", headers={"Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "content-type,idempotency-key,authorization"})
        self.assertEqual(204, status)
        self.assertEqual("http://localhost:3000", headers["access-control-allow-origin"])
        self.assertIn("idempotency-key", headers["access-control-allow-headers"].lower())
        self.assertNotIn("access-control-allow-origin", request(port, headers={"Origin": "https://untrusted.invalid"})[1])

    def test_authorization_headers_correlation_and_safe_logs(self):
        port, _, log = self.gateway("Reconstruction", {
            "ReverseProxy__Clusters__reconstruction-api__Destinations__local__Address": f"http://127.0.0.1:{self.secure_api}/"})
        path = "/api/reconstruccion/proyectos"
        self.assertEqual(401, request(port, path, headers={"X-Actor": "administrator", "X-User-Id": "1"})[0])
        token = "gateway-header-validation-token"
        status, headers, _ = request(port, path, headers={"Authorization": "Bearer " + token})
        self.assertEqual(401, status)
        self.assertIn("invalid_token", headers["www-authenticate"])
        trace = "1" * 32
        _, headers, _ = request(port, headers={"traceparent": f"00-{trace}-{'2' * 16}-01"})
        self.assertEqual(trace, headers["x-trace-id"])
        self.assertNotIn("server", headers)
        self.assertNotIn(token, log.read_text())
        self.assertIn(trace, log.read_text())

    def test_rate_limit_is_per_instance_and_does_not_trust_forwarded_ip(self):
        overrides = {"Traffic__RequestsPerWindow": "2", "Traffic__WindowSeconds": "60"}
        first, _, _ = self.gateway(overrides=overrides)
        second, _, _ = self.gateway(overrides=overrides)
        for i in range(2):
            self.assertEqual(200, request(first, headers={"X-Forwarded-For": f"198.51.100.{i + 1}"})[0])
        status, headers, _ = request(first, headers={"X-Forwarded-For": "203.0.113.1"})
        self.assertEqual(429, status)
        self.assertGreaterEqual(int(headers["retry-after"]), 1)
        self.assertEqual(200, request(first, "/health")[0])
        self.assertEqual(200, request(second)[0])
        trusted, _, _ = self.gateway(overrides={**overrides, "ForwardedHeaders__KnownProxies__0": "127.0.0.1"})
        for _ in range(2):
            self.assertEqual(200, request(trusted, headers={"X-Forwarded-For": "198.51.100.1"})[0])
        self.assertEqual(429, request(trusted, headers={"X-Forwarded-For": "198.51.100.1"})[0])
        self.assertEqual(200, request(trusted, headers={"X-Forwarded-For": "198.51.100.2"})[0])

    def test_hosts_headers_and_request_size_limits(self):
        self.assertEqual(400, request(self.original, headers={"Host": "untrusted.invalid"})[0])
        self.assertEqual(200, request(self.original, headers={"X-Forwarded-Host": "untrusted.invalid",
            "X-Forwarded-Proto": "https", "Forwarded": "for=203.0.113.1;host=untrusted.invalid"})[0])
        port, _, _ = self.gateway(overrides={"Kestrel__Limits__MaxRequestHeadersTotalSize": "1024",
                                           "Kestrel__Limits__MaxRequestBodySize": "1024"})
        self.assertEqual(431, request(port, headers={"X-Padding": "x" * 2048})[0])
        name = str(uuid.uuid4())
        self.assertEqual(413, request(port, method="POST", body={**self.report(name), "padding": "x" * 2048})[0])
        self.assertNotIn(name, [d["nombre"] for d in request(self.original)[2]])

    def test_timeout_and_concurrency_limit(self):
        port, _, _ = self.gateway(overrides={"Traffic__ConcurrentRequests": "1",
                                            "ReverseProxy__Clusters__disasters-api__HttpRequest__ActivityTimeout": "00:00:01"})
        os.kill(self.original_process.pid, signal.SIGSTOP)
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(request, port)
                time.sleep(.2)
                self.assertEqual(200, request(port, "/health")[0])
                self.assertEqual(429, request(port)[0])
                self.assertEqual(504, pending.result(timeout=5)[0])
        finally:
            os.kill(self.original_process.pid, signal.SIGCONT)
        self.assertEqual(200, request(port)[0])

    def test_active_health_and_unavailable_destination(self):
        port, _, _ = self.gateway("Reconstruction", {
            "ReverseProxy__Clusters__reconstruction-api__Destinations__local__Address": f"http://127.0.0.1:{self.secure_api}/",
            "ReverseProxy__Clusters__reconstruction-api__HealthCheck__Active__Interval": "00:00:00.2",
            "ReverseProxy__Clusters__reconstruction-api__HealthCheck__Active__Timeout": "00:00:00.15",
            "ReverseProxy__Clusters__reconstruction-api__Metadata__ConsecutiveFailuresHealthPolicy.Threshold": "1"})
        os.kill(self.secure_process.pid, signal.SIGSTOP)
        try:
            time.sleep(.8)
            self.assertEqual(503, request(port, "/api/reconstruccion/health")[0])
            self.assertEqual(200, request(port, "/health")[0])
        finally:
            os.kill(self.secure_process.pid, signal.SIGCONT)
        wait_ready(port, "/api/reconstruccion/health", self.processes[-1])
        unavailable, _, _ = self.gateway(overrides={
            "ReverseProxy__Clusters__disasters-api__Destinations__local__Address": f"http://127.0.0.1:{free_port()}/"})
        status, _, body = request(unavailable)
        self.assertEqual(502, status)
        self.assertNotIn(b"Exception", body)

    def test_equivalent_destinations_and_gateway_replicas_preserve_idempotency(self):
        settings = {"ReverseProxy__Clusters__disasters-api__Destinations__peer__Address": f"http://127.0.0.1:{self.peer_api}/"}
        first, _, log = self.gateway(overrides=settings)
        second, _, _ = self.gateway(overrides=settings)
        body = {**self.report(), "radio": 500}
        headers = {"Idempotency-Key": str(uuid.uuid4())}
        status, _, saved = request(first, method="POST", body=body, headers=headers)
        self.assertEqual(201, status)
        status, _, replay = request(second, method="POST", body=body, headers=headers)
        self.assertEqual(200, status)
        self.assertEqual(saved, replay)
        for _ in range(6):
            self.assertEqual(saved, request(first, f"/api/desastres/{saved['id']}")[2])
        content = log.read_text()
        self.assertIn("via peer", content)
        self.assertIn("via local", content)

    def test_full_size_attachment_is_forwarded_without_session_affinity(self):
        _, _, disaster = request(self.expanded, method="POST", body={**self.report(), "radio": 500})
        for phase in ["Response", "Recovery"]:
            status, _, disaster = request(self.expanded, f"/api/desastres/{disaster['id']}/fase", "PUT",
                {"phase": phase, "expectedVersion": disaster["version"]})
            self.assertEqual(200, status)
        _, _, proposals = request(self.expanded, "/api/reconstruccion/propuestas/generar", "POST", {"disasterId": disaster["id"]})
        status, _, project = request(self.expanded, "/api/reconstruccion/proyectos", "POST",
            {"proposalId": proposals[0]["id"], "expectedVersion": proposals[0]["version"]})
        self.assertEqual(200, status)
        path = f"/api/reconstruccion/proyectos/{project['id']}/adjuntos"
        boundary = uuid.uuid4().hex
        data = b"%PDF-1.4\n" + b"x" * (20 * 1024 * 1024 - 9)
        raw = (f'--{boundary}\r\nContent-Disposition: form-data; name="category"\r\n\r\nevidence\r\n'
               f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="report.pdf"\r\nContent-Type: application/pdf\r\n\r\n').encode() + data + f'\r\n--{boundary}--\r\n'.encode()
        status, _, attachment = request(self.expanded, path, "POST", raw, {"Content-Type": f"multipart/form-data; boundary={boundary}"})
        self.assertEqual(200, status)
        replica, _, _ = self.gateway("Reconstruction")
        status, _, downloaded = request(replica, path + "/" + attachment["id"])
        self.assertEqual(200, status)
        self.assertEqual(hashlib.sha256(data).digest(), hashlib.sha256(downloaded).digest())


if __name__ == "__main__":
    unittest.main(verbosity=2)
