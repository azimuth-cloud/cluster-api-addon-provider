import asyncio
import unittest
from unittest import mock

import httpx
import kopf
from easykube import ApiError
from pyhelm3 import errors as helm_errors

from capi_addons import operator


def api_error(status_code):
    request = httpx.Request("GET", "https://management.example/clusters/my-cluster")
    response = httpx.Response(
        status_code, request=request, json={"message": "cluster lookup failed"}
    )
    return ApiError(httpx.HTTPStatusError("", request=request, response=response))


class TestAddonDeletion(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cluster = mock.Mock()
        self.cluster.metadata.name = "my-cluster"
        self.cluster.metadata.namespace = "default"
        self.cluster.metadata.get.return_value = None
        self.cluster.status.phase = "Provisioned"

        self.resource = mock.AsyncMock()
        self.resource.fetch.return_value = self.cluster
        self.client = mock.AsyncMock()
        self.client.api_preferred_version.return_value.resource.return_value = (
            self.resource
        )
        create_client = self.enterContext(
            mock.patch.object(operator, "create_ek_client")
        )
        create_client.return_value.__aenter__.return_value = self.client

        secret = self.enterContext(mock.patch.object(operator.k8s, "Secret"))
        self.fetch_secret = mock.AsyncMock()
        secret.return_value.fetch = self.fetch_secret
        self.target_client = mock.Mock()
        self.helm_client = mock.Mock()
        self.clients_for_cluster = self.enterContext(
            mock.patch.object(operator, "clients_for_cluster")
        )
        self.clients_for_cluster.return_value.__aenter__.return_value = (
            self.target_client,
            self.helm_client,
        )

        self.addon = mock.Mock()
        self.addon.metadata.namespace = "default"
        self.addon.spec.cluster_name = "my-cluster"
        self.addon.spec.paused = False
        self.addon.uninstall = mock.AsyncMock()
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def delete_addon(self):
        # Bound the tests so a regression in task cancellation cannot hang CI.
        async with asyncio.timeout(5):
            await operator.handle_addon_deleted(addon=self.addon, logger=mock.Mock())

    async def blocked_uninstall(self, *args):
        self.started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise

    async def test_cluster_already_deleting(self):
        # CAPI may not have updated status.phase when deletion starts.
        self.cluster.metadata.get.return_value = "2026-11-20T10:00:00Z"
        await self.delete_addon()
        self.addon.uninstall.assert_not_awaited()
        self.fetch_secret.assert_not_awaited()

    async def test_cluster_without_status(self):
        del self.cluster.status
        await self.delete_addon()
        self.addon.uninstall.assert_awaited_once()

    async def test_cluster_not_found(self):
        self.resource.fetch.side_effect = api_error(404)
        await self.delete_addon()
        self.addon.uninstall.assert_not_awaited()

    async def test_kubeconfig_not_found(self):
        self.fetch_secret.side_effect = api_error(404)
        await self.delete_addon()
        self.addon.uninstall.assert_not_awaited()

    async def test_paused_addon(self):
        self.addon.spec.paused = True
        await self.delete_addon()
        self.addon.uninstall.assert_not_awaited()

    async def test_uninstall_on_running_cluster(self):
        # The deletion watcher must stop when a normal uninstall finishes.
        with mock.patch.object(operator, "until_cluster_deleted") as watch:
            watch.side_effect = self.blocked_uninstall
            await self.delete_addon()
        self.assertTrue(self.cancelled.is_set())
        self.addon.uninstall.assert_awaited_once_with(
            self.client, self.target_client, self.helm_client
        )

    @mock.patch.object(operator.asyncio, "sleep")
    async def test_wait_for_cluster_deletion(self, sleep):
        deleting = mock.Mock()
        deleting.metadata.get.return_value = "2026-11-20T10:00:00Z"
        self.resource.fetch.side_effect = [self.cluster, deleting]
        await operator.until_cluster_deleted(self.resource, self.cluster)
        sleep.assert_awaited_once_with(5)
        self.assertEqual(self.resource.fetch.await_count, 2)
        self.resource.fetch.assert_awaited_with("my-cluster", namespace="default")

    async def test_cluster_deleted_during_uninstall(self):
        for state in ("deleting", "gone", "forbidden"):
            with self.subTest(state=state):
                self.started.clear()
                self.cancelled.clear()
                self.addon.uninstall.side_effect = self.blocked_uninstall
                first_fetch = True

                async def fetch_cluster(*args, **kwargs):
                    nonlocal first_fetch
                    if first_fetch:
                        first_fetch = False
                        return self.cluster
                    # Simulate a change after Helm uninstall has started.
                    await self.started.wait()
                    if state == "deleting":
                        deleting = mock.Mock()
                        deleting.metadata.get.return_value = "2026-11-20T10:00:00Z"
                        return deleting
                    raise api_error(404 if state == "gone" else 403)

                self.resource.fetch.side_effect = fetch_cluster
                if state == "forbidden":
                    # A failed cluster lookup must not release the finalizer.
                    with self.assertRaises(ApiError):
                        await self.delete_addon()
                else:
                    await self.delete_addon()
                self.assertTrue(self.cancelled.is_set())

    async def test_uninstall_errors(self):
        for error, expected in (
            (api_error(409), kopf.TemporaryError),
            (api_error(403), ApiError),
            (
                helm_errors.ConnectionError(1, b"", b"connection failed"),
                kopf.TemporaryError,
            ),
            (helm_errors.Error(1, b"", b"hook failed"), helm_errors.Error),
        ):
            with self.subTest(error=type(error).__name__):
                self.addon.uninstall.side_effect = error
                with self.assertRaises(expected):
                    await self.delete_addon()

    async def test_release_not_found(self):
        self.addon.uninstall.side_effect = helm_errors.ReleaseNotFoundError(
            1, b"", b"release not found"
        )
        await self.delete_addon()

    async def test_handler_cancelled(self):
        self.addon.uninstall.side_effect = self.blocked_uninstall
        watcher_cancelled = asyncio.Event()
        watcher_started = asyncio.Event()

        async def watch(*args):
            watcher_started.set()
            try:
                await asyncio.Future()
            finally:
                watcher_cancelled.set()

        with mock.patch.object(operator, "until_cluster_deleted", side_effect=watch):
            task = asyncio.create_task(self.delete_addon())
            try:
                async with asyncio.timeout(5):
                    await self.started.wait()
                    await watcher_started.wait()
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
        self.assertTrue(self.cancelled.is_set())
        self.assertTrue(watcher_cancelled.is_set())
