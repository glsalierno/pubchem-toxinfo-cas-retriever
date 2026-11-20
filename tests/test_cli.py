import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

import aiohttp

import get_toxinfo_by_cas6 as lookup


class Response:
    def __init__(self, status, payload=None, retry_after=None):
        self.status = status
        self.body = payload
        self.headers = {"Retry-After": retry_after} if retry_after is not None else {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    async def text(self):
        return self.body if isinstance(self.body, str) else json.dumps(self.body)


class Session:
    def __init__(self, *replies):
        self.replies = iter(replies)
        self.calls = []

    def get(self, url, *, timeout):
        self.calls.append((url, timeout.total))
        reply = next(self.replies)
        if isinstance(reply, Exception):
            raise reply
        return reply


class HttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_retries_429_503_and_applies_30s_total_timeout(self):
        session = Session(Response(429, retry_after="3"), Response(503), Response(200, {"ok": True}))
        with patch.object(lookup.asyncio, "sleep", AsyncMock()) as sleep:
            self.assertEqual(await lookup.fetch_url(session, "https://example.test"), {"ok": True})
        self.assertGreater(sleep.call_args_list[1].args[0], 2.9)
        self.assertTrue(all(timeout == 30 for _, timeout in session.calls))

    async def test_403_is_not_retried_and_is_not_a_missing_cid(self):
        session = Session(Response(403))
        with patch.object(lookup.asyncio, "sleep", AsyncMock()), self.assertLogs(level="WARNING"):
            result = await lookup.process_cas_number(session, "64-17-5")
        self.assertEqual(result, {"CAS": "64-17-5", "error": "HTTP error 403"})
        self.assertEqual(len(session.calls), 1)

    async def test_timeout_invalid_json_and_transport_are_distinct(self):
        with patch.object(lookup.asyncio, "sleep", AsyncMock()), self.assertLogs(level="ERROR"):
            self.assertIn("timed out", (await lookup.fetch_url(Session(asyncio.TimeoutError()), "x"))["error"])
            self.assertIn("JSON", (await lookup.fetch_url(Session(Response(200, "<html>")), "x"))["error"])
            self.assertIn("bad connection", (await lookup.fetch_url(Session(aiohttp.ClientError("bad connection")), "x"))["error"])

    async def test_retry_exhaustion_returns_explicit_error(self):
        session = Session(*[Response(503)] * 3)
        with patch.object(lookup.asyncio, "sleep", AsyncMock()), self.assertLogs(level="WARNING"):
            self.assertEqual(await lookup.fetch_url(session, "x"), {"error": "HTTP error 503"})
        self.assertEqual(len(session.calls), 3)

    async def test_long_retry_after_stops_other_requests(self):
        session = Session(Response(429, retry_after="90"))
        pacing = lookup.PubChemPace()
        with patch.object(lookup.asyncio, "sleep", AsyncMock()), self.assertLogs(level="WARNING"):
            self.assertEqual(await lookup.fetch_url(session, "x", pacing=pacing), {"error": "HTTP error 429"})
            self.assertIn("cooldown", (await lookup.fetch_url(session, "y", pacing=pacing))["error"])
        self.assertEqual(len(session.calls), 1)

    async def test_uses_one_view_payload_per_cid(self):
        session = Session(Response(200, {"IdentifierList": {"CID": [702]}}),
                          Response(200, {"Record": {"Section": []}}),
                          Response(200, {"PropertyTable": {"Properties": [
                              {"ConnectivitySMILES": "CCO", "IUPACName": "ethanol"}]}}))
        with patch.object(lookup.asyncio, "sleep", AsyncMock()):
            result = await lookup.process_cas_number(session, "64-17-5")
        self.assertEqual(result["SMILES"], "CCO")
        self.assertEqual(sum("pug_view" in url for url, _ in session.calls), 1)
