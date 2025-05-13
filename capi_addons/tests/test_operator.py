import unittest

from capi_addons import operator


class TestOperator(unittest.TestCase):
    def test_handle_secret_event(self):
        operator.handle_secret_event(
            type="type", name="name", namespace="namespace", body={"key": "value"}
        )
