import os
import tempfile
import unittest
from unittest import mock

from sshtik import connection
from sshtik.connection import open_jumps, parse_proxyjump, resolve_ssh_config


class TestParseProxyJump(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(parse_proxyjump("bastion"), [("bastion", None, None)])
        self.assertEqual(parse_proxyjump("u@b:2200, c"), [("b", "u", 2200), ("c", None, None)])
        self.assertEqual(parse_proxyjump("ssh://u@b:22/"), [("b", "u", 22)])
        self.assertEqual(parse_proxyjump("[::1]:2222"), [("::1", None, 2222)])
        self.assertEqual(parse_proxyjump("fe80::1"), [("fe80::1", None, None)])

    def test_none(self):
        self.assertEqual(parse_proxyjump(None), [])
        self.assertEqual(parse_proxyjump("none"), [])


class TestResolve(unittest.TestCase):
    def resolve(self, cfg, host):
        with tempfile.TemporaryDirectory() as home:
            os.mkdir(os.path.join(home, ".ssh"))
            with open(os.path.join(home, ".ssh", "config"), "w") as f:
                f.write(cfg)
            with mock.patch.dict(os.environ, {"HOME": home}):
                return resolve_ssh_config(host)

    def test_proxyjump(self):
        r = self.resolve("Host inner\n  ProxyJump j1,u@j2:2200\n", "inner")
        self.assertEqual(r["proxyjump"], [("j1", None, None), ("j2", "u", 2200)])
        self.assertIsNone(r["proxycommand"])

    def test_proxycommand(self):
        r = self.resolve("Host inner\n  ProxyCommand nc -X 5 %h %p\n", "inner")
        self.assertEqual(r["proxyjump"], [])
        self.assertEqual(r["proxycommand"], "nc -X 5 inner 22")

    def test_wildcard_jump_skips_the_jump_host_itself(self):
        cfg = "Host *\n  ProxyJump bastion\n"
        self.assertEqual(self.resolve(cfg, "bastion")["proxyjump"], [])
        self.assertEqual(self.resolve(cfg, "web")["proxyjump"], [("bastion", None, None)])


class FakeConn:
    def __init__(self, host, via):
        self.host, self.via, self.closed = host, via, False

    def close(self):
        self.closed = True


class TestOpenJumps(unittest.TestCase):
    def test_chains_each_hop_through_the_previous(self):
        chain = open_jumps([("a", None, None), ("b", None, None)], lambda h, u, p, v: FakeConn(h, v))
        self.assertEqual([c.host for c in chain], ["a", "b"])
        self.assertIsNone(chain[0].via)
        self.assertIs(chain[1].via, chain[0])

    def test_cancel_closes_opened_hops(self):
        opened = []
        def connect(h, u, p, v):
            if h == "b":
                return None
            opened.append(FakeConn(h, v)); return opened[-1]
        self.assertIsNone(open_jumps([("a", None, None), ("b", None, None)], connect))
        self.assertTrue(opened[0].closed)


class TestSSHConnectionJumps(unittest.TestCase):
    """SSHConnection tunnels through ProxyJump hops and owns them."""
    def setUp(self):
        self.clients = []
        def client():
            c = mock.MagicMock()
            self.clients.append(c)
            return c
        for p in (mock.patch.object(connection, "ensure_ssh_agent"),
                  mock.patch("sshtik.connection.paramiko.SSHClient", client)):
            p.start(); self.addCleanup(p.stop)

    def resolved(self, jumps, proxycommand=None):
        def fake(host, user=None, port=None):
            return {"hostname": host, "user": user, "port": port or 22, "key_filename": None,
                    "proxyjump": jumps if host == "inner" else [],
                    "proxycommand": proxycommand if host == "inner" else None}
        return mock.patch.object(connection, "resolve_ssh_config", fake)

    def test_proxyjump_chain(self):
        with self.resolved([("j1", None, None), ("j2", None, 2200)]):
            conn = connection.SSHConnection("inner")
        self.assertEqual([j.host for j in conn.jumps], ["j1", "j2"])
        self.assertIs(conn.via, conn.jumps[-1])
        self.assertIs(conn.jumps[1].via, conn.jumps[0])
        conn.jumps[0].transport.open_channel.assert_called_once()
        self.assertEqual(conn.jumps[0].transport.open_channel.call_args[0][1], ("j2", 2200))
        self.assertEqual(conn.jumps[1].transport.open_channel.call_args[0][1], ("inner", 22))
        conn.close()
        for c in self.clients:
            c.close.assert_called()

    def test_failed_target_closes_hops(self):
        with self.resolved([("j1", None, None)]):
            orig = connection.paramiko.SSHClient
            def client():
                c = orig()
                if len(self.clients) == 2:
                    c.connect.side_effect = OSError("refused")
                return c
            with mock.patch("sshtik.connection.paramiko.SSHClient", client):
                with self.assertRaises(OSError):
                    connection.SSHConnection("inner")
        self.clients[0].close.assert_called()

    def test_proxycommand(self):
        with self.resolved([], proxycommand="nc inner 22"), \
                mock.patch("sshtik.connection.paramiko.ProxyCommand") as pc:
            conn = connection.SSHConnection("inner")
        pc.assert_called_once_with("nc inner 22")
        self.assertIs(self.clients[-1].connect.call_args.kwargs["sock"], pc.return_value)
        self.assertEqual(conn.jumps, [])


if __name__ == "__main__":
    unittest.main()
