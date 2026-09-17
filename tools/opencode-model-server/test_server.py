import unittest
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import server


class ModelServerTests(unittest.TestCase):
    def test_local_request_accepts_loopback_and_local_interface_address(self):
        self.assertTrue(server.is_local_request("127.0.0.1", "127.0.0.1"))
        self.assertTrue(server.is_local_request("::1", "::1"))
        self.assertTrue(server.is_local_request("::ffff:127.0.0.1", "::"))
        self.assertTrue(server.is_local_request("192.168.1.102", "192.168.1.102"))

    def test_local_request_rejects_other_hosts_and_unspecified_addresses(self):
        self.assertFalse(server.is_local_request("192.168.1.101", "192.168.1.102"))
        self.assertFalse(server.is_local_request("0.0.0.0", "0.0.0.0"))
        self.assertFalse(server.is_local_request("not-an-address", "127.0.0.1"))

    def test_discovery_keeps_zero_cost_live_models_and_orders_active_first(self):
        live = {
            "data": [
                {"id": "paid-model"},
                {"id": "legacy-free"},
                {"id": "active-free"},
            ]
        }
        metadata = {
            "opencode": {
                "models": {
                    "paid-model": {"cost": {"input": 1, "output": 1}},
                    "legacy-free": {
                        "name": "Legacy",
                        "status": "deprecated",
                        "cost": {"input": 0, "output": 0},
                    },
                    "active-free": {
                        "name": "Active",
                        "tool_call": True,
                        "cost": {"input": 0, "output": 0},
                    },
                }
            }
        }

        def fake_fetch(url):
            return live if url == server.ZEN_MODELS_URL else metadata

        with patch.object(server, "fetch_json", side_effect=fake_fetch):
            models, errors = server.discover_models()

        self.assertEqual(errors, {})
        self.assertEqual([model["id"] for model in models], ["active-free", "legacy-free"])
        self.assertTrue(models[0]["free"])
        self.assertEqual(models[1]["status"], "deprecated")

    def test_model_config_is_exposed_without_losing_metadata(self):
        model = server.compact_model(
            "active-free",
            {"name": "Active", "cost": {"input": 0, "output": 0}, "limit": {"context": 123}},
            {"id": "active-free", "created": 42},
        )
        self.assertEqual(model["config"]["limit"]["context"], 123)
        self.assertEqual(model["created"], 42)


if __name__ == "__main__":
    unittest.main()
