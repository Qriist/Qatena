"""stdio MCP smoke tests (SPEC 3): spawn `python -m qatena`, drive JSON-RPC line-by-line.

Every readline carries a thread-based deadline so a hung server fails the test
instead of hanging the suite.
"""
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from helpers import REPO_ROOT

EXPECTED_TOOLS = {
    "chain_put", "chain_delete", "chain_split", "chain_merge",
    "blade_insert", "blade_edit", "blade_move", "blade_delete",
    "batch", "pointer_add", "pointer_remove",
    "get", "search", "list", "pointers_of", "follow", "history",
}


class ServerSmoke(unittest.TestCase):
    DEADLINE = 15  # seconds per line / wait

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="qatena-srv-")
        env = dict(os.environ)
        env["PYTHONPATH"] = REPO_ROOT
        env["QATENA_DB"] = os.path.join(self._tmp, "qatena.db")
        env["PYTHONIOENCODING"] = "utf-8"
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "qatena"],
            cwd=REPO_ROOT, env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8",
        )
        self._next_id = 0
        self._lines = queue.Queue()

        def _pump():
            # One thread owns stdout; the test thread pulls lines with
            # deadlines off the queue, so readers never race.
            try:
                for line in self.proc.stdout:
                    self._lines.put(line)
            except Exception as e:  # noqa: BLE001
                self._lines.put(e)
            self._lines.put(None)  # EOF sentinel

        threading.Thread(target=_pump, daemon=True).start()

    def tearDown(self):
        if self.proc.poll() is None:
            try:
                self.proc.kill()
            except OSError:
                pass
        try:
            self.proc.wait(timeout=5)
        except Exception:
            pass
        shutil.rmtree(self._tmp, ignore_errors=True)

    # -- transport helpers ---------------------------------------------------
    def _readline_deadlined(self, timeout):
        """Fetch one queued stdout line; fail the test on timeout (hang).

        The blocking read itself happens in the daemon pump thread started in
        setUp; this deadline lives on the queue pull, so a hung server fails
        the test instead of hanging the suite.
        """
        try:
            item = self._lines.get(timeout=timeout)
        except queue.Empty:
            self.fail("server produced no line within %ss (hang)" % timeout)
        if item is None:
            self.fail("server closed stdout early")
        if isinstance(item, Exception):
            self.fail("stdout read error: %r" % item)
        return item

    def _peek_deadlined(self, timeout):
        """Like _readline_deadlined but returns None on timeout instead of failing."""
        try:
            item = self._lines.get(timeout=timeout)
        except queue.Empty:
            return None
        if isinstance(item, Exception):
            self.fail("stdout read error: %r" % item)
        return None if item is None else item

    def _send(self, obj):
        self.proc.stdin.write(json.dumps(obj) + "\n")
        self.proc.stdin.flush()

    def _request(self, method, params=None):
        self._next_id += 1
        msg_id = self._next_id
        self._send({"jsonrpc": "2.0", "id": msg_id, "method": method,
                    "params": params or {}})
        line = self._readline_deadlined(self.DEADLINE)
        self.assertIsNotNone(line, "server closed stdout early")
        msg = json.loads(line)
        self.assertEqual(msg.get("id"), msg_id,
                         "unexpected line for %r: %r" % (method, line))
        return msg

    def _call_tool(self, name, arguments=None):
        res = self._request("tools/call",
                            {"name": name, "arguments": arguments or {}})
        self.assertNotIn("error", res, "JSON-RPC error calling %r: %r" % (name, res))
        return res["result"]

    # -- the smoke sequence ----------------------------------------------------
    def test_stdio_smoke(self):
        with self.subTest("initialize echoes protocolVersion and names qatena"):
            res = self._request("initialize", {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "smoke", "version": "0"},
            })
            self.assertNotIn("error", res)
            r = res["result"]
            self.assertEqual(r["serverInfo"]["name"], "qatena")
            self.assertEqual(r["protocolVersion"], "2025-06-18")

        with self.subTest("notifications/initialized produces no line; ping -> {}"):
            self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            time.sleep(0.2)
            res = self._request("ping")
            self.assertEqual(res["result"], {})
            # nothing is queued for the notification itself
            self.assertIsNone(self._peek_deadlined(1.0),
                              "notifications/initialized produced a response line")

        with self.subTest("tools/list returns exactly the 17 SPEC verbs"):
            res = self._request("tools/list")
            tools = res["result"]["tools"]
            self.assertEqual(len(tools), 17)
            self.assertEqual({t["name"] for t in tools}, EXPECTED_TOOLS)
            for t in tools:
                self.assertIn("inputSchema", t)
                self.assertIn("description", t)

        with self.subTest("tools/call chain_put then get round-trips"):
            r1 = self._call_tool("chain_put", {"name": "The Forge"})
            self.assertFalse(r1["isError"])
            body1 = json.loads(r1["content"][0]["text"])
            cid = body1["id"]
            self.assertEqual(body1["name"], "The Forge")
            r2 = self._call_tool("get", {"id": cid})
            self.assertFalse(r2["isError"])
            self.assertEqual(json.loads(r2["content"][0]["text"])["name"],
                             "The Forge")

        with self.subTest("verb-level error surfaces as isError with error JSON"):
            r = self._call_tool("get",
                                {"id": "00000000-0000-7000-8000-000000000000"})
            self.assertTrue(r["isError"])
            self.assertEqual(json.loads(r["content"][0]["text"])["error"]["code"],
                             "not_found")

        with self.subTest("unknown verb name -> isError with not_found"):
            r = self._call_tool("definitely_not_a_verb")
            self.assertTrue(r["isError"])
            body = json.loads(r["content"][0]["text"])
            self.assertEqual(body["error"]["code"], "not_found")

        with self.subTest("unknown JSON-RPC method -> -32601"):
            res = self._request("no/such/method")
            self.assertEqual(res["error"]["code"], -32601)

        with self.subTest("malformed line -> -32700 with null id"):
            self.proc.stdin.write("this is not json\n")
            self.proc.stdin.flush()
            msg = json.loads(self._readline_deadlined(self.DEADLINE))
            self.assertEqual(msg["error"]["code"], -32700)
            self.assertIsNone(msg.get("id"))

        with self.subTest("stdin close -> clean exit 0"):
            self.proc.stdin.close()
            self.proc.wait(timeout=self.DEADLINE)
            self.assertEqual(self.proc.returncode, 0)
