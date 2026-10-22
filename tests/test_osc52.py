import base64
import unittest

from sshtik import osc52


def seq(text, sel=b"c", end=b"\x07"):
    return b"\x1b]52;" + sel + b";" + base64.b64encode(text.encode()) + end


class TestScanner(unittest.TestCase):
    def test_bel_and_st_terminators(self):
        s = osc52.Scanner()
        self.assertEqual(s.feed(b"before" + seq("hi") + b"after"), [("clipboard", "hi")])
        self.assertEqual(s.feed(seq("yo", end=b"\x1b\\")), [("clipboard", "yo")])

    def test_split_across_reads(self):
        s = osc52.Scanner()
        data = b"xx" + seq("split ünïcode") + b"yy"
        found = []
        for i in range(len(data)):  # one byte at a time
            found += s.feed(data[i:i + 1])
        self.assertEqual(found, [("clipboard", "split ünïcode")])

    def test_two_in_one_read(self):
        s = osc52.Scanner()
        self.assertEqual(s.feed(seq("a") + seq("b", sel=b"p")), [("clipboard", "a"), ("primary", "b")])

    def test_empty_selection_param_means_clipboard(self):
        self.assertEqual(osc52.Scanner().feed(seq("t", sel=b"")), [("clipboard", "t")])

    def test_query_and_garbage_ignored(self):
        s = osc52.Scanner()
        self.assertEqual(s.feed(b"\x1b]52;c;?\x07"), [])  # never answer a read
        self.assertEqual(s.feed(b"\x1b]52;c;not base64!!\x07"), [])
        self.assertEqual(s.feed(b"\x1b]52;c;\x07"), [])
        self.assertEqual(s.feed(b"\x1b]52;nosemicolon\x07"), [])
        self.assertEqual(s.feed(b"\x1b]0;window title\x07plain text"), [])
        self.assertEqual(s.feed(seq("still works")), [("clipboard", "still works")])

    def test_over_long_sequence_dropped_then_recovers(self):
        s = osc52.Scanner()
        s.feed(b"\x1b]52;c;" + b"A" * (osc52.MAX_PAYLOAD + 10))
        self.assertEqual(s.feed(b"AAAA" * 100), [])
        self.assertEqual(s.feed(b"\x07" + seq("next")), [("clipboard", "next")])
        self.assertLess(len(s._pending), 64)


if __name__ == "__main__":
    unittest.main()
