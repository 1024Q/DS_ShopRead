import unittest
from unittest.mock import AsyncMock
from fastapi import HTTPException
from backend.center.shop_transfer import stop_confirmed


class StopConfirmationTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_needs_no_extra_request(self):
        remote = AsyncMock(return_value={"state": "stopped"})
        await stop_confirmed(remote, "source", 7)
        remote.assert_awaited_once_with("source", "POST", "shops/7/stop", {})

    async def test_lost_reply_verified_on_live_source_without_replay(self):
        for status in (502, 503, 504):
            remote = AsyncMock(side_effect=[HTTPException(status, "lost reply"), [{"id": 7, "state": "stopped"}]])
            await stop_confirmed(remote, "source", 7)
            self.assertEqual(remote.await_count, 2)
            self.assertEqual(remote.await_args.args, ("source", "GET", "shops"))

    async def test_wrong_shop_running_missing_or_unreachable_never_confirms(self):
        for value in (
            [{"id": 8, "state": "stopped"}],
            [{"id": 7, "state": "logged_in"}],
            [],
            HTTPException(503, "offline"),
        ):
            remote = AsyncMock(side_effect=[HTTPException(504, "timeout"), value])
            with self.assertRaises(HTTPException) as caught:
                await stop_confirmed(remote, "source", 7)
            self.assertEqual(caught.exception.status_code, 409)
            self.assertEqual(remote.await_count, 2)

    async def test_explicit_refusal_is_not_overridden_by_state(self):
        remote = AsyncMock(side_effect=HTTPException(400, "cannot close browser"))
        with self.assertRaises(HTTPException):
            await stop_confirmed(remote, "source", 7)
        self.assertEqual(remote.await_count, 1)
