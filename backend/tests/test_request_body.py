import unittest
from fastapi import HTTPException
from starlette.requests import Request
from backend.file_upload import read_body


class BodyReuseTests(unittest.IsolatedAsyncioTestCase):
    def request(self, parts):
        chunks = iter(parts)

        async def receive():
            value = next(chunks)
            return {"type": "http.request", "body": value[0], "more_body": value[1]}

        return Request({"type": "http", "method": "PUT", "path": "/", "headers": []}, receive)

    async def test_fanout_can_read_same_validated_request_twice(self):
        request = self.request([(b'{"max_', True), (b'concurrent":10}', False)])
        expected = b'{"max_concurrent":10}'
        self.assertEqual(await read_body(request, 100), expected)
        self.assertEqual(await read_body(request, 100), expected)
        self.assertEqual(await request.body(), expected)

    async def test_cached_body_still_obeys_smaller_limit(self):
        request = self.request([(b"12345", False)])
        await request.body()
        with self.assertRaises(HTTPException) as error:
            await read_body(request, 4)
        self.assertEqual(error.exception.status_code, 413)

    async def test_overflow_never_caches_partial_payload(self):
        request = self.request([(b"123", True), (b"456", False)])
        with self.assertRaises(HTTPException) as error:
            await read_body(request, 5)
        self.assertEqual(error.exception.status_code, 413)
        self.assertFalse(hasattr(request, "_body"))
