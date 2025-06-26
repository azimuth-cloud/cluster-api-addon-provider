import hashlib
import unittest
from unittest import mock

from capi_addons import operator


# Helper to create an async iterable
class AsyncIterList:
    def __init__(self, items):
        self.items = items

    def __aiter__(self):
        return self._generator()

    async def _generator(self):
        for item in self.items:
            yield item


class MockResource:
    def __init__(self, items):
        self._items = items
        self.list_kwargs = None
        self.patch = mock.AsyncMock()

    def list(self, **kwargs):
        self.list_kwargs = kwargs
        return AsyncIterList(self._items)


class TestOperator(unittest.IsolatedAsyncioTestCase):
    @mock.patch("capi_addons.operator.HelmClient")
    @mock.patch("capi_addons.operator.Configuration")
    @mock.patch("capi_addons.operator.tempfile.NamedTemporaryFile")
    @mock.patch("capi_addons.operator.base64.b64decode")
    async def test_clients_for_cluster(
        self, mock_b64decode, mock_tempfile, mock_config_class, mock_helm_class
    ):
        # Prepare base64-decoded kubeconfig data
        kubeconfig_data = b"apiVersion: v1\nclusters:\n..."
        encoded_value = hashlib.sha256(kubeconfig_data).hexdigest()  # dummy content
        kubeconfig_secret = mock.Mock()
        kubeconfig_secret.data = {"value": encoded_value}

        # Mock base64 decoding
        mock_b64decode.return_value = kubeconfig_data

        # Mock temp file
        mock_temp = mock.MagicMock()
        mock_temp.name = "/mock/kubeconfig"
        mock_temp.__enter__.return_value = mock_temp
        mock_tempfile.return_value.__enter__.return_value = mock_temp

        # Mock EasyKube client
        mock_async_client = mock.AsyncMock()
        mock_config = mock.Mock()
        mock_config.async_client.return_value = mock_async_client
        mock_config_class.from_kubeconfig_data.return_value = mock_config

        # Mock Helm client
        mock_helm_instance = mock.Mock()
        mock_helm_class.return_value = mock_helm_instance

        # Patch settings
        operator.settings.easykube_field_manager = "test-manager"
        operator.settings.helm_client = mock.Mock(
            default_timeout=60,
            executable="helm",
            history_max_revisions=10,
            insecure_skip_tls_verify=True,
            unpack_directory="/mock/unpack",
        )

        async with operator.clients_for_cluster(kubeconfig_secret) as (
            ek_client,
            helm_client,
        ):
            mock_b64decode.assert_called_once()
            mock_config_class.from_kubeconfig_data.assert_called_once_with(
                kubeconfig_data, json_encoder=mock.ANY
            )
            mock_config.async_client.assert_called_once_with(
                default_field_manager="test-manager"
            )
            mock_helm_class.assert_called_once_with(
                default_timeout=60,
                executable="helm",
                history_max_revisions=10,
                insecure_skip_tls_verify=True,
                kubeconfig="/mock/kubeconfig",
                unpack_directory="/mock/unpack",
            )
            self.assertEqual(ek_client, mock_async_client)
            self.assertEqual(helm_client, mock_helm_instance)

        mock_async_client.__aenter__.assert_called_once()
        mock_async_client.__aexit__.assert_called_once()

    async def test_fetch_ref(self):
        ref = {"name": "my-secret"}
        default_namespace = "default"

        mock_resource = mock.AsyncMock()
        mock_resource.fetch.return_value = {
            "kind": "Secret",
            "metadata": {"name": "my-secret"},
        }

        mock_api = mock.Mock()
        mock_api.resource = mock.AsyncMock(return_value=mock_resource)

        mock_ek_client = mock.Mock()
        mock_ek_client.api.return_value = mock_api

        result = await operator.fetch_ref(mock_ek_client, ref, default_namespace)

        mock_ek_client.api.assert_called_once_with("v1")
        mock_api.resource.assert_awaited_once_with("Secret")
        mock_resource.fetch.assert_awaited_once_with("my-secret", namespace="default")
        self.assertEqual(result["metadata"]["name"], "my-secret")

    @mock.patch("capi_addons.operator.asyncio.sleep", new_callable=mock.AsyncMock)
    @mock.patch("capi_addons.operator.create_ek_client")
    async def test_until_deleted(self, mock_create_ek_client, mock_sleep):
        addon = mock.Mock()
        addon.api_version = "group/v1"
        addon.metadata.name = "my-addon"
        addon.metadata.namespace = "my-namespace"
        addon._meta.plural_name = "addons"

        alive_obj = mock.Mock()
        alive_obj.metadata.get.return_value = None

        deleting_obj = mock.Mock()
        deleting_obj.metadata.get.return_value = "2024-01-01T00:00:00Z"

        mock_resource = mock.AsyncMock()
        mock_resource.fetch = mock.AsyncMock(side_effect=[alive_obj, deleting_obj])

        mock_ekapi = mock.Mock()
        mock_ekapi.resource = mock.AsyncMock(return_value=mock_resource)

        mock_ek_client = mock.AsyncMock()
        mock_ek_client.api = mock.Mock(return_value=mock_ekapi)

        mock_create_ek_client.return_value.__aenter__.return_value = mock_ek_client

        await operator.until_deleted(addon)

        self.assertEqual(mock_resource.fetch.await_count, 2)
        mock_resource.fetch.assert_called_with("my-addon", namespace="my-namespace")

    async def test_compute_checksum(self):
        data = {"key1": "value1", "key2": "value2"}
        expected_checksum = hashlib.sha256(b"key1=value1;key2=value2").hexdigest()
        result = operator.compute_checksum(data)
        self.assertEqual(result, expected_checksum)

        modified_data = {"key1": "value1", "key2": "DIFFERENT"}
        result_modified = operator.compute_checksum(modified_data)
        self.assertNotEqual(result, result_modified)

        empty_checksum = operator.compute_checksum({})
        expected_empty_checksum = hashlib.sha256(b"").hexdigest()
        self.assertEqual(empty_checksum, expected_empty_checksum)

    @mock.patch("capi_addons.operator.registry")
    async def test_handle_config_event_deleted(self, mock_registry):
        addon_metadata = mock.Mock()
        addon_metadata.name = "deleted-addon"
        addon_metadata.namespace = "test-namespace"
        addon_metadata.annotations = {}

        addon = mock.Mock()
        addon.metadata = addon_metadata

        addon_obj = mock.Mock()
        mock_registry.get_model_instance.return_value = addon

        # Mock registry to return one CRD
        crd = mock.Mock()
        crd.api_group = "example.group"
        crd.plural_name = "addons"
        crd.versions = {"v1": mock.Mock(storage=True)}
        mock_registry.__iter__.return_value = iter([crd])

        # Use MockResource and AsyncIterList
        mock_resource = MockResource(items=[addon_obj])

        mock_ekapi = mock.Mock()
        mock_ekapi.resource = mock.AsyncMock(return_value=mock_resource)

        mock_ek_client = mock.Mock()
        mock_ek_client.api.return_value = mock_ekapi

        # Call the method with type="DELETED"
        await operator.handle_config_event(
            ek_client=mock_ek_client,
            type="DELETED",
            name="test-config",
            namespace="test-namespace",
            body={"data": {"foo": "bar"}},  # ignored for DELETED
            annotation_prefix="secret.addons.stackhpc.com",
        )

        self.assertIn(
            "test-config.secret.addons.stackhpc.com/uses",
            mock_resource.list_kwargs["labels"],
        )

        mock_resource.patch.assert_awaited_once()

        patched_annotations = mock_resource.patch.call_args[0][1]["metadata"][
            "annotations"
        ]
        self.assertEqual(
            patched_annotations["secret.addons.stackhpc.com/test-config"], "deleted"
        )

    @mock.patch("capi_addons.operator.registry")
    async def test_handle_config_event_not_deleted(self, mock_registry):
        # Prepare mock addon metadata with no matching annotation initially
        addon_metadata = mock.Mock()
        addon_metadata.name = "addon-1"
        addon_metadata.namespace = "test-namespace"
        addon_metadata.annotations = {}

        # Mock addon object returned from registry.get_model_instance
        addon = mock.Mock()
        addon.metadata = addon_metadata

        addon_obj = mock.Mock()
        mock_registry.get_model_instance.return_value = addon

        # Mock registry to return one CRD with version storage
        crd = mock.Mock()
        crd.api_group = "example.group"
        crd.plural_name = "addons"
        crd.versions = {"v1": mock.Mock(storage=True)}
        mock_registry.__iter__.return_value = iter([crd])

        mock_resource = MockResource(items=[addon_obj])

        mock_ekapi = mock.Mock()
        mock_ekapi.resource = mock.AsyncMock(return_value=mock_resource)

        mock_ek_client = mock.Mock()
        mock_ek_client.api.return_value = mock_ekapi

        # Define config data and expected checksum
        data = {"foo": "bar", "baz": "qux"}
        data_str = ";".join(sorted(f"{k}={v}" for k, v in data.items())).encode()
        expected_checksum = hashlib.sha256(data_str).hexdigest()

        # Call the function with type != "DELETED"
        await operator.handle_config_event(
            ek_client=mock_ek_client,
            type="MODIFIED",
            name="test-config",
            namespace="test-namespace",
            body={"data": data},
            annotation_prefix="secret.addons.stackhpc.com",
        )

        # Assertions to check resource.list was called with correct labels and namespace
        self.assertEqual(
            mock_resource.list_kwargs["labels"],
            {"test-config.secret.addons.stackhpc.com/uses": operator.PRESENT},
        )
        self.assertEqual(mock_resource.list_kwargs["namespace"], "test-namespace")

        mock_resource.patch.assert_awaited_once()

        # Verify patched annotation value matches expected checksum
        patch_args = mock_resource.patch.call_args[0]
        patch_body = patch_args[1]
        annotation_key = "secret.addons.stackhpc.com/test-config"
        self.assertEqual(
            patch_body["metadata"]["annotations"][annotation_key],
            expected_checksum,
        )

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
