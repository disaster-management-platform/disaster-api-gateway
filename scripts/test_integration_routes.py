"""Check explicit integration routing and deny accidental diagnostic/provider exposure."""
import json
import unittest
from pathlib import Path


class IntegrationRoutes(unittest.TestCase):
    def test_route_contract(self):
        root = Path(__file__).resolve().parents[1]
        proxy = json.loads((root / "DisasterLocation.Gateway/appsettings.json").read_text())["ReverseProxy"]
        routes = [r for r in proxy["Routes"].values() if r["ClusterId"] == "integration-api"]
        expected = {
            "/api/organizaciones": {"GET", "POST"},
            "/api/organizaciones/tipos": {"GET"},
            "/api/organizaciones/{id:long}": {"GET", "PUT"},
            "/api/personas-afectadas/resumen": {"GET"},
            "/api/personas-afectadas": {"GET", "POST"},
            "/api/personas-afectadas/{id:long}": {"GET", "PUT"},
            "/api/coordinacion/tareas": {"GET", "POST"},
            "/api/coordinacion/tareas/{id:long}": {"PUT"},
            "/api/coordinacion/actividades": {"GET", "POST"},
            "/api/coordinacion/actividades/{id:long}": {"PUT"},
            "/api/integracion/reportes/{category:regex(^(monitoring|gis|hospital|administrative|financial)$)}": {"POST"},
        }
        self.assertEqual(len(expected), len(routes))
        self.assertEqual(expected, {r["Match"]["Path"]: set(r["Match"]["Methods"]) for r in routes})
        self.assertTrue(all(r["MaxRequestBodySize"] <= 262144 and r["RateLimiterPolicy"] == "api" for r in routes))
        self.assertEqual("/api/integracion/diagnostico/ready", proxy["Clusters"]["integration-api"]["HealthCheck"]["Active"]["Path"])


if __name__ == "__main__":
    unittest.main()
