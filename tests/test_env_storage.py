"""Baseline snapshots never store credentials from container environments.

The lab's own manifest sets ORDER_DB_PASSWORD as a literal env value; it
must never reach a snapshot on disk.
"""

import unittest

from src.core.env_names import is_endpoint_variable, storable_endpoint_value
from src.core.models import ServiceEnvironment

ORDER = "urn:release-coord:acme:prod:aks-1:shop:makeline-service"


class StorableValueTests(unittest.TestCase):
    def test_credentials_are_stripped_or_withheld(self) -> None:
        cases = {
            "mongodb://user:s3cret@documentdb:10260/orderdb?tls=true": "mongodb://documentdb:10260/orderdb",
            "amqp://guest:guest@rabbitmq:5672/": "amqp://rabbitmq:5672/",
            "https://api.example.com/v1?sig=abc&sv=2024": "https://api.example.com/v1",
            "http://payments": "http://payments",
            "rabbitmq": "rabbitmq",
            "Server=sql;Database=db;Password=hunter2": None,
            "Endpoint=sb://bus.servicebus.windows.net/;SharedAccessKey=abc": None,
            "redis:6379,password=abc": None,
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(storable_endpoint_value(value), expected)

    def test_endpoint_names(self) -> None:
        self.assertTrue(is_endpoint_variable("ORDER_DB_URI"))
        self.assertFalse(is_endpoint_variable("ORDER_DB_PASSWORD"))
        self.assertFalse(is_endpoint_variable("API_KEY"))


class ForStorageTests(unittest.TestCase):
    def test_only_clean_endpoint_values_are_kept(self) -> None:
        environment = ServiceEnvironment(
            service_id=ORDER,
            variables={
                "ORDER_DB_URI": "mongodb://admin:pw@documentdb:10260",
                "ORDER_DB_PASSWORD": "pw",
                "ORDER_QUEUE_HOSTNAME": "rabbitmq",
                "SB_CONNECTION_STRING": "Endpoint=sb://x/;SharedAccessKey=k",
                "LEGACY_DSN": "Server=sql;Password=pw",
            },
            variable_sources={"ORDER_DB_PASSWORD": ["deployment/m/app:env"],
                              "ORDER_QUEUE_HOSTNAME": ["deployment/m/app:env"]},
        )
        stored = environment.for_storage()

        self.assertEqual(
            stored.variables,
            {"ORDER_DB_URI": "mongodb://documentdb:10260", "ORDER_QUEUE_HOSTNAME": "rabbitmq"},
        )
        self.assertEqual(
            stored.withheld_variables, ["LEGACY_DSN", "ORDER_DB_PASSWORD", "SB_CONNECTION_STRING"]
        )
        self.assertEqual(list(stored.variable_sources), ["ORDER_QUEUE_HOSTNAME"])
        self.assertNotIn("pw", stored.model_dump_json())
        # Withheld names still count as set.
        self.assertFalse(stored.is_unset("LEGACY_DSN"))
        self.assertTrue(stored.is_unset("PAYMENTS_URL"))
