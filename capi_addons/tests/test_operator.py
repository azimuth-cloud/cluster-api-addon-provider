import unittest
from unittest import mock

from capi_addons import operator


class TestOperator(unittest.IsolatedAsyncioTestCase):
    @mock.patch.object(operator, "handle_config_event")
    @mock.patch.object(operator, "create_ek_client")
    async def test_handle_secret_event(
        self, mock_create_ek_client, mock_handle_config_event
    ):
        operator.ek_config = mock.AsyncMock()

        await operator.handle_secret_event(
            type="type", name="name", namespace="namespace", body={"key": "value"}
        )

        mock_handle_config_event.assert_awaited_once_with(
            mock.ANY,
            "type",
            "name",
            "namespace",
            {"key": "value"},
            "secret.addons.stackhpc.com",
        )
        mock_create_ek_client.assert_called_once_with()
