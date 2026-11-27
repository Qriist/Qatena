"""stdio MCP smoke tests (SPEC 1.9, 3): spawn `python -m qatena` with a temp
QATENA_ARMORY dir, drive newline-delimited JSON-RPC over stdio.

Every readline carries a thread-based deadline so a hung server fails the test
instead of hanging the suite. QATENA_DB is retired (SPEC 1.9) and is explicitly
popped from the spawn env; routing is QATENA_ARMORY + the vambrace key.
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

from qatena.uuid7 import new_uuid7
from helpers import REPO_ROOT, UUID7_RE

ROUTED_TOOLS = {
    "chain_put", "chain_delete", "chain_split", "chain_merge",
    "blade_insert", "blade_edit", "blade_move", "blade_delete",
    "batch", "reblade_add", "reblade_remove", "open_item",
    "get", "search", "list", "reblades_of", "follow", "history",
}
EXPECTED_TOOLS = ROUTED_TOOLS | {"vambrace"}


class ServerSmoke(unittest.TestCase):
    DEADLINE = 15  # seconds per line / wait

    def setUp(self):
        self._armory = tempfile.mkdtemp(prefix="qatena-armories-")
        self._servers = []
        self.proc, self._lines = self._spawn()
        self._next_id = 0

    def tearDown(self):
        for proc in self._servers:
            if proc.poll() is None:
                try:
                    proc.kill()
                except OSError:
                    pass
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
        shutil.rmtree(self._armory, ignore_errors=True)

    # -- spawn ---------------------------------------------------------------
    def _env(self):
        env = dict(os.environ)
        env.pop("QATENA_DB", None)  # retired: routing is QATENA_ARMORY + keys
        env["PYTHONPATH"] = REPO_ROOT
        env["QATENA_ARMORY"] = self._armory
        env["PYTHONIOENCODING"] = "utf-8"
        return env

    def _spawn(self):
        """Spawn a server against the shared temp armory dir and rebind the
        transport state (self.proc / self._lines) to it, so tests that kill
        the server can respawn with the same QATENA_ARMORY."""
        proc = subprocess.Popen(
            [sys.executable, "-m", "qatena"],
            cwd=REPO_ROOT, env=self._env(),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8",
        )
        self._servers.append(proc)
        lines = queue.Queue()

        def _pump():
            # One thread owns stdout; the test thread pulls lines with
            # deadlines off the queue, so readers never race.
            try:
                for line in proc.stdout:
                    lines.put(line)
            except Exception as e:  # noqa: BLE001
                lines.put(e)
            lines.put(None)  # EOF sentinel

        threading.Thread(target=_pump, daemon=True).start()
        self.proc, self._lines = proc, lines
        return proc, lines

    # -- transport helpers ---------------------------------------------------
    def _readline_deadlined(self, timeout):
        """Fetch one queued stdout line; fail the test on timeout (hang).

        The blocking read itself happens in the daemon pump thread started in
        _spawn; this deadline lives on the queue pull, so a hung server fails
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

    # -- verb helpers ----------------------------------------------------------
    def _code(self, result):
        """Error code of a tool result (isError=True payload)."""
        return json.loads(result["content"][0]["text"])["error"]["code"]

    def _create_vambrace(self):
        """Call the vambrace verb and return the minted routing key."""
        r = self._call_tool("vambrace", {"action": "equip"})
        self.assertFalse(r["isError"], "vambrace equip failed: %r" % r)
        body = json.loads(r["content"][0]["text"])
        key = body["vambrace"]
        self.assertRegex(key, UUID7_RE)
        self.assertTrue(os.path.exists(os.path.join(self._armory, key + ".db")),
                        "vambrace file %s.db missing from %s" % (key, self._armory))
        return key

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

        with self.subTest("tools/list returns 19 tools; 18 routed tools require vambrace"):
            res = self._request("tools/list")
            tools = res["result"]["tools"]
            self.assertEqual(len(tools), 19)
            self.assertEqual({t["name"] for t in tools}, EXPECTED_TOOLS)
            for t in tools:
                self.assertIn("inputSchema", t)
                self.assertIn("description", t)
            for t in tools:
                props = t["inputSchema"]["properties"]
                if t["name"] == "vambrace":
                    # equip is key-less (the key must NOT be required); the two
                    # key-bearing autoexport actions take it as an optional arg
                    self.assertEqual(props.get("vambrace", {}).get("type"), "string")
                    self.assertNotIn("vambrace", t["inputSchema"]["required"])
                    self.assertIn("action", t["inputSchema"]["required"])
                else:
                    self.assertEqual(props.get("vambrace", {}).get("type"), "string")
                    self.assertIn("vambrace", t["inputSchema"]["required"])

        with self.subTest("vambrace equip -> exactly {'vambrace': uuid7} + bootstrapped file"):
            r = self._call_tool("vambrace", {"action": "equip"})
            self.assertFalse(r["isError"], "vambrace equip failed: %r" % r)
            body = json.loads(r["content"][0]["text"])
            self.assertEqual(list(body), ["vambrace"],
                             "equip returns exactly the routing key: %r" % body)
            key = body["vambrace"]
            self.assertRegex(key, UUID7_RE)
            self.assertEqual(key[14], "7")
            self.assertTrue(os.path.exists(os.path.join(self._armory, key + ".db")))

        with self.subTest("routed chain_put then get round-trips"):
            r1 = self._call_tool("chain_put", {"vambrace": key, "name": "The Forge"})
            self.assertFalse(r1["isError"], "chain_put failed: %r" % r1)
            body1 = json.loads(r1["content"][0]["text"])
            self.assertEqual(body1["name"], "The Forge")
            self.assertIn("log_id", body1)
            r2 = self._call_tool("get", {"vambrace": key, "id": body1["id"]})
            self.assertFalse(r2["isError"])
            self.assertEqual(json.loads(r2["content"][0]["text"])["name"],
                             "The Forge")

        with self.subTest("verb-level error surfaces as isError with error JSON"):
            r = self._call_tool("get",
                                {"vambrace": key,
                                 "id": "00000000-0000-7000-8000-000000000000"})
            self.assertTrue(r["isError"])
            self.assertEqual(self._code(r), "not_found")

        with self.subTest("unknown verb: routing checked first, then the verb layer"):
            r = self._call_tool("definitely_not_a_verb")
            self.assertTrue(r["isError"])
            self.assertEqual(self._code(r), "missing_vambrace")
            r = self._call_tool("definitely_not_a_verb", {"vambrace": key})
            self.assertTrue(r["isError"])
            self.assertEqual(self._code(r), "not_found")

        with self.subTest("unknown JSON-RPC method -> -32601"):
            res = self._request("no/such/method")
            self.assertEqual(res["error"]["code"], -32601)

        with self.subTest("malformed line -> -32700 with null id"):
            self.proc.stdin.write("this is not json\n")
            self.proc.stdin.flush()
            msg = json.loads(self._readline_deadlined(self.DEADLINE))
            self.assertEqual(msg["error"]["code"], -32700)
            self.assertIsNone(msg.get("id"))

    def test_fail_closed_matrix(self):
        with self.subTest("missing key (absent, empty, non-string) -> missing_vambrace"):
            for args in ({}, {"vambrace": ""}, {"vambrace": None},
                         {"vambrace": 42}, {"vambrace": ["k"]}):
                r = self._call_tool("chain_put", args)
                self.assertTrue(r["isError"], "args %r should fail closed" % args)
                self.assertEqual(self._code(r), "missing_vambrace")

        with self.subTest("malformed key -> malformed_vambrace"):
            v7 = new_uuid7()
            bad = (
                "not-a-uuid",       # garbage
                "abc",              # too short
                v7.upper(),         # uppercase hex
                v7[:14] + "4" + v7[15:],   # version-4 nibble
                v7 + "0",           # 37 chars
                v7[:9] + v7[10:],   # missing hyphen
            )
            for k in bad:
                r = self._call_tool("chain_put", {"vambrace": k})
                self.assertTrue(r["isError"], "key %r should fail closed" % k)
                self.assertEqual(self._code(r), "malformed_vambrace")

        with self.subTest("well-formed key, no vambrace file -> unknown_vambrace"):
            ghost = new_uuid7()  # never created via the vambrace verb
            r = self._call_tool("chain_put", {"vambrace": ghost})
            self.assertTrue(r["isError"])
            self.assertEqual(self._code(r), "unknown_vambrace")

    def test_vambrace_unsupported_actions(self):
        with self.subTest("any action but equip -> unsupported_action"):
            for action in ("list", "destroy", "CREATE"):
                r = self._call_tool("vambrace", {"action": action})
                self.assertTrue(r["isError"])
                self.assertEqual(self._code(r), "unsupported_action")

        with self.subTest("missing action -> unsupported_action"):
            r = self._call_tool("vambrace", {})
            self.assertTrue(r["isError"])
            self.assertEqual(self._code(r), "unsupported_action")

        with self.subTest("stray routing key on the verb itself is ignored"):
            r = self._call_tool("vambrace",
                                {"action": "equip", "vambrace": new_uuid7()})
            self.assertFalse(r["isError"])
            self.assertRegex(json.loads(r["content"][0]["text"])["vambrace"], UUID7_RE)

    def test_batch_takes_key_once_at_top_level(self):
        key = self._create_vambrace()
        r = self._call_tool("batch", {
            "vambrace": key,
            "ops": [
                {"op": "chain_put", "args": {"name": "Batch A"}},
                {"op": "chain_put", "args": {"name": "Batch B"}},
            ],
        })
        self.assertFalse(r["isError"], "batch failed: %r" % r)
        body = json.loads(r["content"][0]["text"])
        self.assertEqual(len(body["ops"]), 2)
        self.assertIn("log_id", body)  # one transaction, one minted log
        self.assertEqual(sorted(op["name"] for op in body["ops"]),
                         ["Batch A", "Batch B"])
        # round-trip one of the batched chains through the same key
        cid = body["ops"][0]["id"]
        r2 = self._call_tool("get", {"vambrace": key, "id": cid})
        self.assertFalse(r2["isError"])
        self.assertEqual(json.loads(r2["content"][0]["text"])["name"], "Batch A")
        # batch without the key fails closed at the routing layer
        r3 = self._call_tool("batch",
                             {"ops": [{"op": "chain_put", "args": {"name": "X"}}]})
        self.assertTrue(r3["isError"])
        self.assertEqual(self._code(r3), "missing_vambrace")

    def test_routing_survives_server_restart(self):
        key = self._create_vambrace()
        r = self._call_tool("chain_put",
                            {"vambrace": key, "name": "Across Restart"})
        self.assertFalse(r["isError"])
        cid = json.loads(r["content"][0]["text"])["id"]

        # kill the first server; routing state is the vambrace files themselves,
        # so a respawned server with the same QATENA_ARMORY routes the same key
        self.proc.kill()
        self.proc.wait(timeout=self.DEADLINE)
        self._spawn()  # rebind self.proc / self._lines to the respawn

        r = self._call_tool("get", {"vambrace": key, "id": cid})
        self.assertFalse(r["isError"])
        self.assertEqual(json.loads(r["content"][0]["text"])["name"],
                         "Across Restart")

    def test_stdin_close_clean_exit(self):
        res = self._request("ping")
        self.assertEqual(res["result"], {})
        self.proc.stdin.close()
        self.proc.wait(timeout=self.DEADLINE)
        self.assertEqual(self.proc.returncode, 0)
