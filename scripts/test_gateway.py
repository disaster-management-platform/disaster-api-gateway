"""Gateway checks with real APIs, temporary PostgreSQL databases and Node.js for shared frontend requests."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, closing, contextmanager
import psycopg2
from psycopg2 import sql
import hashlib
import re
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import signal
import shutil
import urllib.request
import urllib.error
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
    if method == "PUT" and path.endswith("/fase") and isinstance(body, dict):
        body = {"reason": "Transición autorizada del escenario Gateway", "confirmNoCriticalCommitments": True, **body}
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
        cls.connection = connection
        with tempfile.TemporaryFile(mode="w+") as log:
            result = subprocess.run(["dotnet", str(BACKEND / "bin/Debug/net10.0/DisasterLocation.dll"),
                    "--Database:Migrate=true"], cwd=BACKEND,
                    env={**os.environ, "ConnectionStrings__Disasters": connection}, stdout=log, stderr=log, timeout=60)
            if result.returncode:
                log.seek(0)
                raise AssertionError(log.read()[-6000:])
        cls.disaster_api, cls.first_process, _ = cls.start(BACKEND, {
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
        cls.first, _, _ = cls.gateway()
        cls.second, _, _ = cls.gateway()

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
    def gateway(cls, overrides=None):
        env = {"ASPNETCORE_ENVIRONMENT": "Development",
               "ReverseProxy__Clusters__disasters-api__Destinations__local__Address":
                   f"http://127.0.0.1:{cls.disaster_api}/",
               "ReverseProxy__Clusters__reconstruction-api__Destinations__local__Address":
                   f"http://127.0.0.1:{cls.reconstruction_api}/"}
        return cls.start(GATEWAY, {**env, **(overrides or {})}, "/health")

    @staticmethod
    def report(name=None):
        return {"nombre": name or str(uuid.uuid4()), "ubicacion": "Bogotá", "severidad": 4,
                "descripcion": "Gateway validation", "latitud": 4.7, "longitud": -74.0}

    def test_reports_organizations_geography_and_alerts_use_explicit_routes(self):
        status, _, report = request(self.first, "/api/reportes-desastre", "POST", {
            "sourceKind": "External", "sourceId": "test-canonical-provider", "sourceReportId": str(uuid.uuid4()),
            "content": {"measurement": 3}, "createPreliminary": True, "disaster": self.report()})
        self.assertEqual(201, status)
        disaster_id = report["disasterId"]
        self.assertEqual(report["id"], request(self.first, f"/api/reportes-desastre/{report['id']}")[2]["id"])
        self.assertEqual("Detected", request(self.second, f"/api/desastres/{disaster_id}")[2]["phase"])
        self.assertEqual(report["id"], request(self.second, f"/api/desastres/{disaster_id}/reportes")[2][0]["id"])
        self.assertEqual(200, request(self.first, f"/api/desastres/{disaster_id}/organismos", "PUT", {
            "expectedVersion": 1, "registeringOrganizationId": "eden:registrar:1", "emergencyResponsibleId": "person:4",
            "organizations": [{"organizationId": "eden:12", "responsibility": "Rescate"}]})[0])
        self.assertEqual(200, request(self.first, f"/api/desastres/{disaster_id}/geografia", "PUT", {
            "expectedVersion": 2, "sourceId": "gis:canonical", "geography": {"polygon": {"type": "Polygon",
                "coordinates": [[[-74.1, 4.6], [-74.0, 4.6], [-74.0, 4.7], [-74.1, 4.6]]]}, "municipalities": ["Bogotá"]}})[0])
        alerts = request(self.second, f"/api/desastres/{disaster_id}/alertas")[2]
        self.assertEqual(["DisasterDetected", "DisasterAreaChanged"], [x["eventType"] for x in alerts])
        self.assertEqual(404, request(self.first, f"/api/desastres/{disaster_id}/outbox-internal")[0])

    def test_batch_delta_limits_and_replica_retries_preserve_individual_outcomes(self):
        operation = {"clientRequestId": str(uuid.uuid4()), "disaster": self.report()}
        body = {"operations": [operation]}
        status, _, first = request(self.first, "/api/desastres/batch", "POST", body)
        self.assertEqual(200, status); self.assertEqual("Created", first["results"][0]["status"])
        status, _, repeated = request(self.second, "/api/desastres/batch", "POST", body)
        self.assertEqual(200, status); self.assertEqual("AlreadyProcessed", repeated["results"][0]["status"])
        self.assertEqual(first["results"][0]["disaster"], repeated["results"][0]["disaster"])
        self.assertEqual(400, request(self.first, "/api/desastres/batch", "POST", {"operations": [operation] * 51})[0])
        self.assertEqual(413, request(self.first, "/api/desastres/batch", "POST", {"operations": [], "padding": "x" * 1048576})[0])
        self.assertEqual(200, request(self.first, "/api/desastres/sync?limit=1")[0])
        self.assertEqual(400, request(self.first, "/api/desastres/sync?limit=51")[0])
        limited, _, _ = self.gateway({"Traffic__BatchOperationBudget": "50"})
        self.assertEqual(200, request(limited, "/api/desastres/batch", "POST", body)[0])
        status, headers, _ = request(limited, "/api/desastres/batch", "POST", body)
        self.assertEqual(429, status); self.assertIn("retry-after", headers)
        self.assertEqual(200, request(limited)[0])

    def test_bulk_concurrency_and_timeouts_leave_interactive_capacity_available(self):
        values = dict(re.findall(r'([^;=]+)="((?:[^"]|"")*)"', self.connection))
        settings = {target: values[source].replace('""', '"') for source, target in
                    [("Host", "host"), ("Port", "port"), ("Database", "dbname"), ("Username", "user")] if source in values}
        body = lambda: {"operations": [{"clientRequestId": str(uuid.uuid4()), "disaster": self.report()}]}
        with closing(psycopg2.connect(**settings)) as lock:
            with lock.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_xact_lock(6041,1)")
                with ThreadPoolExecutor(2) as pool:
                    pending = [pool.submit(request, self.first, "/api/desastres/batch", "POST", body()) for _ in range(2)]
                    deadline = time.monotonic() + 5
                    with closing(psycopg2.connect(**settings)) as monitor, monitor.cursor() as state:
                        while True:
                            state.execute("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND wait_event='advisory'")
                            if state.fetchone()[0] >= 2: break
                            self.assertLess(time.monotonic(), deadline)
                            monitor.commit(); time.sleep(.05)
                    self.assertEqual(429, request(self.first, "/api/desastres/batch", "POST", body())[0])
                    self.assertEqual(200, request(self.first)[0])
                    lock.commit()
                    self.assertEqual([200, 200], [future.result()[0] for future in pending])
                timed, _, _ = self.gateway({"ReverseProxy__Routes__disaster-batch__Timeout": "00:00:00.2"})
                cursor.execute("SELECT pg_advisory_xact_lock(6041,1)")
                operation = body()
                timeout_result = request(timed, "/api/desastres/batch", "POST", operation)
                self.assertEqual(504, timeout_result[0], timeout_result[2])
                self.assertEqual(200, request(self.first)[0])
                lock.commit()
                self.assertEqual(200, request(self.second, "/api/desastres/batch", "POST", operation)[0])

    def test_replicas_share_contracts_frontend_and_disasters(self):
        original_body = self.report()
        expanded_body = {**self.report(), "radio": 500}
        status, _, original = request(self.first, method="POST", body=original_body)
        self.assertEqual(201, status)
        self.assertEqual(set(original_body) | {"id", "radio", "phase", "version", "disasterTypeId", "startedAt", "registeredAt", "updatedAt", "attentionLevel"}, set(original))
        self.assertIsNone(original["radio"])
        status, _, expanded = request(self.second, method="POST", body=expanded_body)
        self.assertEqual(201, status)
        self.assertEqual(set(expanded_body) | {"id", "phase", "version", "disasterTypeId", "startedAt", "registeredAt", "updatedAt", "attentionLevel"}, set(expanded))
        self.assertIn(expanded_body["nombre"], [d["nombre"] for d in request(self.first)[2]])
        self.assertIn(original_body["nombre"], [d["nombre"] for d in request(self.second)[2]])
        self.assertEqual(original, request(self.second, f"/api/desastres/{original['id']}")[2])
        self.assertEqual(expanded, request(self.first, f"/api/desastres/{expanded['id']}")[2])
        self.assertEqual(200, request(self.first, f"/api/desastres/{original['id']}/fase", "PUT",
                                     {"phase": "Response", "expectedVersion": 1})[0])
        self.assertEqual(200, request(self.second, f"/api/desastres/{expanded['id']}/fase", "PUT",
                                     {"phase": "Response", "expectedVersion": expanded["version"]})[0])
        key = {"Idempotency-Key": str(uuid.uuid4())}
        status, _, shared = request(self.first, method="POST", body=self.report("shared-" + str(uuid.uuid4())), headers=key)
        self.assertEqual(201, status)
        shared_body = {field: shared[field] for field in original_body}
        status, _, repeated = request(self.second, method="POST", body={**shared_body, "radio": None}, headers=key)
        self.assertEqual(200, status)
        self.assertEqual(shared, repeated)
        self.assertEqual(409, request(self.second, method="POST", body={**shared_body, "radio": 500}, headers=key)[0])
        self.assertEqual(shared, request(self.first, f"/api/desastres/{shared['id']}")[2])
        for port in [self.first, self.second]:
            self.assertEqual(request(port, "/index.html")[2], request(port, "/")[2])
            for asset in (GATEWAY / "wwwroot").iterdir():
                if asset.is_file():
                    self.assertEqual(asset.read_bytes(), request(port, "/" + asset.name)[2])

    def test_shared_frontend_preserves_anonymous_and_authenticated_requests(self):
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
      sessionStorage: {getItem() { return authenticated ? 'frontend-header-test' : null; }}});
    vm.runInContext(fs.readFileSync('wwwroot/session.js', 'utf8'), context);
    vm.runInContext(api, context);
    await vm.runInContext("api('')", context);
    assert.equal(calls[0].path, '/api/desastres');
    assert.equal(calls[0].headers.get('Authorization'), authenticated ? 'Bearer frontend-header-test' : null);
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
        subprocess.run(["node", "-"], input=script, text=True, cwd=GATEWAY, check=True,
            env={**os.environ, "FRONTEND_CONTEXTS": json.dumps([(self.first, False), (self.second, True)])})

    def test_browser_radius_map_and_retry_identity(self):
        driver = shutil.which("geckodriver")
        if driver is None:
            self.skipTest("Firefox/geckodriver no están disponibles para la prueba de navegador.")
        port = free_port()
        log = open(self.folder / "browser.log", "w+")
        self.logs.append(log)
        process = subprocess.Popen([driver, "--port", str(port)], stdout=log, stderr=log)
        self.processes.append(process)
        def webdriver(method, path, payload=None):
            data = json.dumps(payload).encode() if payload is not None else None
            req = urllib.request.Request(f"http://127.0.0.1:{port}" + path, data=data,
                method=method, headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=60) as response:
                    return json.load(response)["value"]
            except urllib.error.HTTPError as error:
                self.fail(error.read().decode())
        deadline = time.monotonic() + 30
        while True:
            try:
                webdriver("GET", "/status")
                break
            except (OSError, urllib.error.URLError):
                if process.poll() is not None or time.monotonic() > deadline:
                    self.fail("WebDriver no inició: " + (self.folder / "browser.log").read_text()[-2000:])
                time.sleep(.1)
        session = webdriver("POST", "/session", {"capabilities": {"alwaysMatch": {
            "browserName": "firefox", "pageLoadStrategy": "eager", "moz:firefoxOptions": {"args": ["-headless"]}}}})["sessionId"]
        prefix = "/session/" + session
        def script(source, *args):
            return webdriver("POST", prefix + "/execute/sync", {"script": source, "args": list(args)})
        def wait_script(source, *args):
            deadline = time.monotonic() + 20
            while not script(source, *args):
                if time.monotonic() > deadline:
                    self.fail("El navegador no confirmó: " + source)
                time.sleep(.1)
        try:
            webdriver("POST", prefix + "/url", {"url": f"http://127.0.0.1:{self.first}/"})
            wait_script("return typeof L !== 'undefined' && !document.querySelector('#refresh').disabled")
            name = "Browser " + str(uuid.uuid4())
            script("""
window.submissions = [];
const send = window.platformFetch;
window.platformFetch = async (path, options = {}) => {
  const response = await send(path, options);
  if (options.method === 'POST') {
    submissions.push({key: options.headers['Idempotency-Key'], body: JSON.parse(options.body), status: response.status});
    if (submissions.length === 1) throw new TypeError('Respuesta perdida');
  }
  return response;
};
for (const [field, value] of Object.entries({nombre: arguments[0], ubicacion:'Bogotá', severidad:'4',
  descripcion:'Prueba web', latitud:'4.7', longitud:'-74', radio:'1234.75'}))
  document.getElementById(field).value = value;
document.querySelector('#submit').click();
""", name)
            wait_script("return document.querySelector('#form-status').textContent === 'Respuesta perdida'")
            first = script("return submissions[0]")
            self.assertTrue(script("return typeof crypto.randomUUID === 'function' && window.isSecureContext"))
            script("document.querySelector('#nombre').value = 'Cambio incierto'; document.querySelector('#submit').click()")
            self.assertTrue(script("return submissions.length === 1 && document.querySelector('#form-status').textContent.includes('anterior sigue sin confirmar')"))
            webdriver("POST", prefix + "/url", {"url": f"http://127.0.0.1:{self.first}/"})
            wait_script("return !document.querySelector('#refresh').disabled")
            self.assertEqual(name, script("return document.querySelector('#nombre').value"))
            script("""
window.submissions = [arguments[0]];
window.originalPlatformFetch = window.platformFetch;
const send = window.platformFetch;
window.platformFetch = async (path, options = {}) => {
  const response = await send(path, options);
  if (options.method === 'POST') submissions.push({key: options.headers['Idempotency-Key'], body: JSON.parse(options.body), status: response.status});
  return response;
};
document.querySelector('#form-status + button').click();
""", first)
            wait_script("return document.querySelector('#form-status').textContent.includes('registrado correctamente') && !document.querySelector('#refresh').disabled")
            self.assertIsNone(script("return sessionStorage.getItem('disaster-pending-submission')"))
            calls = script("return submissions")
            self.assertEqual([201, 200], [item["status"] for item in calls])
            self.assertEqual(calls[0]["key"], calls[1]["key"])
            self.assertEqual(1234.75, calls[0]["body"]["radio"])
            self.assertEqual(1, len([d for d in request(self.second)[2] if d["nombre"] == name]))
            self.assertTrue(script("return window.eval(arguments[0])", "markers.getLayers().some(layer => layer instanceof L.Circle && layer.getRadius() === 1234.75)"))
            script("""
for (const [field, value] of Object.entries({nombre: arguments[0], ubicacion:'Bogotá', severidad:'4',
  descripcion:'Prueba web', latitud:'4.7', longitud:'-74', radio:'0'})) document.getElementById(field).value = value;
document.querySelector('#submit').click();
""", name + " unknown")
            self.assertTrue(script("return document.querySelector('#form-status').dataset.error === 'true' && submissions.length === 2"))
            script("Object.defineProperty(crypto, 'randomUUID', {value: undefined}); document.querySelector('#radio').value = ''; document.querySelector('#submit').click()")
            wait_script("return submissions.length === 3 && !document.querySelector('#refresh').disabled")
            self.assertIsNone(script("return submissions[2].body.radio"))
            self.assertNotEqual(calls[0]["key"], script("return submissions[2].key"))
            self.assertEqual(4, uuid.UUID(script("return submissions[2].key")).version)
            unknown = next(d for d in request(self.second)[2] if d["nombre"] == name + " unknown")
            self.assertIsNone(unknown["radio"])
            self.assertTrue(script("return window.eval(arguments[0])", "markers.getLayers().some(layer => !(layer instanceof L.Circle) && layer.getPopup().getContent().textContent.includes(" + json.dumps(name + " unknown") + "))"))
            script("window.platformFetch = window.originalPlatformFetch")
            conflict_body = {**self.report(name + " conflict"), "radio": None}
            conflict_key = str(uuid.uuid4())
            self.assertEqual(201, request(self.first, method="POST", body={**conflict_body, "descripcion": "Ya registrado"},
                headers={"Idempotency-Key": conflict_key})[0])
            script("sessionStorage.setItem('disaster-pending-submission', JSON.stringify(arguments[0]))",
                {"key": conflict_key, "content": json.dumps(conflict_body, separators=(",", ":"))})
            webdriver("POST", prefix + "/url", {"url": f"http://127.0.0.1:{self.first}/"})
            wait_script("return !document.querySelector('#refresh').disabled")
            script("document.querySelector('#form-status + button').click()")
            wait_script("return JSON.parse(sessionStorage.getItem('disaster-pending-submission')).conflict === true")
            self.assertEqual(conflict_key, script("return JSON.parse(sessionStorage.getItem('disaster-pending-submission')).key"))
            script("document.querySelector('#form-status + button').click()")
            webdriver("POST", prefix + "/alert/accept", {})
            self.assertIsNone(script("return sessionStorage.getItem('disaster-pending-submission')"))
            script("document.querySelector('#submit').click()")
            wait_script("return document.querySelector('#form-status').textContent.includes('registrado correctamente') && !document.querySelector('#refresh').disabled")
            self.assertEqual(2, len([item for item in request(self.first)[2] if item["nombre"] == conflict_body["nombre"]]))
            for fault in ["id", "nombre"]:
                operation_name = name + " invalid " + fault
                script("""
window.originalPlatformFetch = window.platformFetch;
window.platformFetch = async (path, options = {}) => {
  const response = await originalPlatformFetch(path, options);
  if (options.method === 'POST' && path === '/api/desastres') {
    const body = await response.json(); body[arguments[0]] = arguments[0] === 'id' ? 0 : 'Incompatible';
    return new Response(JSON.stringify(body), {status: response.status, headers: {'Content-Type': 'application/json'}});
  }
  return response;
};
document.querySelector('#disaster-form').reset();
for (const [field, value] of Object.entries({nombre: arguments[1], ubicacion:'Bogotá', severidad:'4',
  descripcion:'Prueba web', latitud:'4.7', longitud:'-74', radio:'321.5'})) document.getElementById(field).value = value;
document.querySelector('#submit').click();
""", fault, operation_name)
                wait_script("return document.querySelector('#form-status').textContent.includes('no confirma el contenido original')")
                pending = script("return JSON.parse(sessionStorage.getItem('disaster-pending-submission'))")
                webdriver("POST", prefix + "/url", {"url": f"http://127.0.0.1:{self.first}/"})
                wait_script("return !document.querySelector('#refresh').disabled")
                self.assertEqual(pending["key"], script("return JSON.parse(sessionStorage.getItem('disaster-pending-submission')).key"))
                script("document.querySelector('#form-status + button').click()")
                wait_script("return document.querySelector('#form-status').textContent.includes('registrado correctamente') && !document.querySelector('#refresh').disabled")
                self.assertEqual(1, len([item for item in request(self.first)[2] if item["nombre"] == operation_name]))
            type_name = name + " type"
            script("document.querySelector('#type-name').value = arguments[0]; document.querySelector('#type-form button').click()", type_name)
            wait_script("return document.querySelector('#type-status').textContent === 'Tipo agregado.'")
            wait_script("return Array.from(document.querySelector('#disasterTypeId').options).some(option => option.textContent === arguments[0])", type_name)
            type_id = script("return Array.from(document.querySelector('#disasterTypeId').options).find(option => option.textContent === arguments[0]).value", type_name)
            managed_name = name + " managed"
            precise_body = {**self.report(managed_name), "radio": 1100, "disasterTypeId": int(type_id), "attentionLevel": 1,
                "startedAt": "2026-11-17T12:00:00.123456Z"}
            script("sessionStorage.setItem('disaster-pending-submission', JSON.stringify(arguments[0]))",
                {"key": str(uuid.uuid4()), "content": json.dumps(precise_body, separators=(",", ":"))})
            webdriver("POST", prefix + "/url", {"url": f"http://127.0.0.1:{self.first}/"})
            wait_script("return !document.querySelector('#refresh').disabled")
            script("document.querySelector('#form-status + button').click()")
            wait_script("return document.querySelector('#form-status').textContent.includes('registrado correctamente') && !document.querySelector('#refresh').disabled")
            managed = next(item for item in request(self.first)[2] if item["nombre"] == managed_name)
            self.assertTrue(managed["startedAt"].endswith(".123456+00:00") or managed["startedAt"].endswith(".123456Z"))
            # Re-render with the catalogue deliberately absent, then refresh it in place.
            script("window.eval('disasterTypes = []'); render(arguments[0], false)", request(self.first)[2])
            self.assertEqual(int(type_id), managed["disasterTypeId"])
            script("""
const article = Array.from(document.querySelectorAll('.report')).find(node => node.querySelector('h3').textContent === arguments[0]);
const edit = article.querySelector('form'); edit.elements.namedItem('nombre').value = 'Edición que se conserva';
window.catalogFetch = window.platformFetch;
window.platformFetch = async (path, options) => {
  if (path === '/api/tipos-desastre') await new Promise(resolve => setTimeout(resolve, 100));
  return catalogFetch(path, options);
};
loadTypes();
""", managed_name)
            wait_script("return window.eval('disasterTypes.length > 0') && Array.from(document.querySelectorAll('select[data-type-selector]')).every(select => Array.from(select.options).some(option => option.value === arguments[0]))", type_id)
            self.assertTrue(script("const article = Array.from(document.querySelectorAll('.report')).find(node => node.querySelector('h3').textContent === arguments[0]); return article.querySelector('form').elements.namedItem('nombre').value === 'Edición que se conserva'", managed_name))
            script("window.platformFetch = window.catalogFetch")
            script("""
const article = Array.from(document.querySelectorAll('.report')).find(node => node.querySelector('h3').textContent === arguments[0]);
const details = article.querySelector('details'); details.open = true;
const edit = details.querySelector('form'); edit.elements.namedItem('nombre').value = arguments[0] + ' updated';
edit.elements.namedItem('attentionLevel').value = '4'; edit.querySelector('button').click();
""", managed_name)
            wait_script("return Array.from(document.querySelectorAll('.report h3')).some(node => node.textContent === arguments[0])", managed_name + " updated")
            current = request(self.first, f"/api/desastres/{managed['id']}")[2]
            self.assertEqual(2, current["version"]); self.assertEqual(4, current["attentionLevel"])
            self.assertEqual(managed["startedAt"], current["startedAt"])
            script("""
const article = Array.from(document.querySelectorAll('.report')).find(node => node.querySelector('h3').textContent === arguments[0]);
const details = article.querySelectorAll('details')[1]; details.open = true;
const phase = details.querySelector('form'); phase.querySelector('select').value = 'Closed';
phase.querySelector('textarea').value = 'Cierre autorizado sin compromisos críticos pendientes';
phase.querySelector('input[type=checkbox]').checked = true; phase.querySelector('button').click();
""", managed_name + " updated")
            wait_script("const article = Array.from(document.querySelectorAll('.report')).find(node => node.querySelector('h3').textContent === arguments[0]); return article && Array.from(article.querySelectorAll('p')).some(node => node.textContent.includes(' · Closed · v3'));", managed_name + " updated")
            closed = request(self.first, f"/api/desastres/{managed['id']}")[2]; self.assertEqual("Closed", closed["phase"])
            script("""
const article = Array.from(document.querySelectorAll('.report')).find(node => node.querySelector('h3').textContent === arguments[0]);
Array.from(article.querySelectorAll('details')).find(item => item.querySelector('summary').textContent === 'Consultar historial').open = true;
""", managed_name + " updated")
            wait_script("return document.querySelector('#disaster-list').textContent.includes('commitmentsConfirmed')")
            script("Array.from(document.querySelectorAll('#type-list p')).find(node => node.textContent.includes(arguments[0])).querySelector('button').click()", type_name)
            wait_script("return Array.from(document.querySelectorAll('#type-list p')).some(node => node.textContent.includes(arguments[0]) && node.textContent.includes('Inactivo'))", type_name)
            self.assertEqual(int(type_id), request(self.first, f"/api/desastres/{managed['id']}")[2]["disasterTypeId"])
            for page in ["reconstruction.html", "access.html", "monitoring.html", "formalization.html", "final-report.html"]:
                webdriver("POST", prefix + "/url", {"url": f"http://127.0.0.1:{self.second}/{page}"})
                self.assertTrue(script("return !!document.querySelector('main')"))
        finally:
            webdriver("DELETE", prefix)

    def test_custom_destination_names_do_not_keep_implicit_local_destinations(self):
        for environment in ["Development", "Production"]:
            with self.subTest(environment=environment):
                expected = request(self.disaster_api)[2]
                env = {"ASPNETCORE_ENVIRONMENT": environment,
                       "ReverseProxy__Clusters__disasters-api__Destinations__external__Address": f"http://127.0.0.1:{self.disaster_api}/",
                       "ReverseProxy__Clusters__reconstruction-api__Destinations__external__Address": f"http://127.0.0.1:{self.reconstruction_api}/"}
                port, _, log = self.start(GATEWAY, env, "/health")
                for _ in range(4):
                    self.assertEqual((200, expected), (request(port)[0], request(port)[2]))
                    self.assertEqual(request(self.reconstruction_api, "/api/reconstruccion/proyectos")[2],
                        request(port, "/api/reconstruccion/proyectos")[2])
                self.assertNotIn("via local", log.read_text())

    def test_missing_destinations_do_not_fall_back_to_local_apis(self):
        for environment in ["Development", "Production"]:
            with self.subTest(environment=environment):
                port, _, _ = self.start(GATEWAY, {"ASPNETCORE_ENVIRONMENT": environment}, "/health")
                self.assertEqual(503, request(port)[0])
                self.assertEqual(503, request(port, "/api/reconstruccion/proyectos")[0])

    def test_routes_methods_queries_and_cors(self):
        port, _, _ = self.gateway(overrides={"Cors__AllowedOrigins__0": "http://localhost:3000"})
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
        port, _, log = self.gateway(overrides={
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
        deadline = time.monotonic() + 5
        while trace not in log.read_text() and time.monotonic() < deadline:
            time.sleep(.05)
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
        self.assertEqual(400, request(self.first, headers={"Host": "untrusted.invalid"})[0])
        self.assertEqual(200, request(self.first, headers={"X-Forwarded-Host": "untrusted.invalid",
            "X-Forwarded-Proto": "https", "Forwarded": "for=203.0.113.1;host=untrusted.invalid"})[0])
        port, _, _ = self.gateway(overrides={"Kestrel__Limits__MaxRequestHeadersTotalSize": "1024",
                                           "Kestrel__Limits__MaxRequestBodySize": "1024"})
        self.assertEqual(431, request(port, headers={"X-Padding": "x" * 2048})[0])
        name = str(uuid.uuid4())
        self.assertEqual(413, request(port, method="POST", body={**self.report(name), "padding": "x" * 2048})[0])
        self.assertNotIn(name, [d["nombre"] for d in request(self.first)[2]])

    def test_timeout_and_concurrency_limit(self):
        port, _, _ = self.gateway(overrides={"Traffic__ConcurrentRequests": "1",
                                            "ReverseProxy__Clusters__disasters-api__HttpRequest__ActivityTimeout": "00:00:01"})
        os.kill(self.first_process.pid, signal.SIGSTOP)
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(request, port)
                time.sleep(.2)
                self.assertEqual(200, request(port, "/health")[0])
                self.assertEqual(429, request(port)[0])
                self.assertEqual(504, pending.result(timeout=5)[0])
        finally:
            os.kill(self.first_process.pid, signal.SIGCONT)
        self.assertEqual(200, request(port)[0])

    def test_active_health_and_unavailable_destination(self):
        port, _, _ = self.gateway(overrides={
            "ReverseProxy__Clusters__reconstruction-api__Destinations__local__Address": f"http://127.0.0.1:{self.secure_api}/",
            "ReverseProxy__Clusters__reconstruction-api__HealthCheck__Active__Interval": "00:00:00.2",
            "ReverseProxy__Clusters__reconstruction-api__HealthCheck__Active__Timeout": "00:00:00.15",
            "ReverseProxy__Clusters__reconstruction-api__Metadata__ConsecutiveFailuresHealthPolicy.Threshold": "1"})
        os.kill(self.secure_process.pid, signal.SIGSTOP)
        try:
            time.sleep(.8)
            self.assertEqual(503, request(port, "/api/reconstruccion/health")[0])
            self.assertEqual(200, request(port, "/health")[0])
            self.assertEqual(200, request(port)[0])
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
        _, _, disaster = request(self.second, method="POST", body={**self.report(), "radio": 500})
        for phase in ["Response", "Recovery"]:
            status, _, disaster = request(self.second, f"/api/desastres/{disaster['id']}/fase", "PUT",
                {"phase": phase, "expectedVersion": disaster["version"]})
            self.assertEqual(200, status)
        _, _, proposals = request(self.second, "/api/reconstruccion/propuestas/generar", "POST", {"disasterId": disaster["id"]})
        status, _, project = request(self.second, "/api/reconstruccion/proyectos", "POST",
            {"proposalId": proposals[0]["id"], "expectedVersion": proposals[0]["version"]})
        self.assertEqual(200, status)
        path = f"/api/reconstruccion/proyectos/{project['id']}/adjuntos"
        boundary = uuid.uuid4().hex
        data = b"%PDF-1.4\n" + b"x" * (20 * 1024 * 1024 - 9)
        raw = (f'--{boundary}\r\nContent-Disposition: form-data; name="category"\r\n\r\nevidence\r\n'
               f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="report.pdf"\r\nContent-Type: application/pdf\r\n\r\n').encode() + data + f'\r\n--{boundary}--\r\n'.encode()
        status, _, attachment = request(self.second, path, "POST", raw, {"Content-Type": f"multipart/form-data; boundary={boundary}"})
        self.assertEqual(200, status)
        replica, _, _ = self.gateway()
        status, _, downloaded = request(replica, path + "/" + attachment["id"])
        self.assertEqual(200, status)
        self.assertEqual(hashlib.sha256(data).digest(), hashlib.sha256(downloaded).digest())


class EvidenceRoutingTests(unittest.TestCase):
    def test_explicit_evidence_routes_reach_the_existing_cluster(self):
        routes=json.loads((GATEWAY / "appsettings.json").read_text())["ReverseProxy"]["Routes"]
        expected={"evidence-collection":["GET","POST"],"evidence-detail":["GET"],"evidence-confirm":["POST"],"evidence-download":["GET"]}
        for name,methods in expected.items():
            self.assertEqual(methods,routes[name]["Match"]["Methods"])
            self.assertEqual("damage-assessment-api",routes[name]["ClusterId"])
            self.assertNotIn("**",routes[name]["Match"]["Path"])
        port=free_port()
        with tempfile.TemporaryFile() as log:
            process=subprocess.Popen(["dotnet",str(GATEWAY / "bin/Debug/net10.0/DisasterLocation.Gateway.dll"),"--urls",f"http://127.0.0.1:{port}"],cwd=GATEWAY,
                env={**os.environ,"ASPNETCORE_ENVIRONMENT":"Development","ReverseProxy__Clusters__damage-assessment-api__Destinations__local__Address":"http://127.0.0.1:8170/"},stdout=log,stderr=log)
            try:
                wait_ready(port,"/health",process)
                path=f"/api/evaluaciones/{uuid.uuid4()}/evidencias"
                for suffix,method in [("","GET"),("","POST"),(f"/{uuid.uuid4()}","GET"),(f"/{uuid.uuid4()}/confirmar","POST"),(f"/{uuid.uuid4()}/descarga","GET")]:
                    self.assertEqual(401,request(port,path+suffix,method)[0])
                self.assertEqual(404,request(port,path+"/internal-storage-key")[0])
            finally:
                process.terminate();process.wait(timeout=10)


if __name__ == "__main__":
    unittest.main(verbosity=2)
