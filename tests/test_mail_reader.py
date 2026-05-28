import unittest

from mail_reader import extract_code, parse_timestamp


class MailReaderTests(unittest.TestCase):
    def test_extract_code_finds_six_digit_code(self):
        self.assertEqual(extract_code("Your code is 285107."), "285107")
        self.assertEqual(extract_code("x1234567y"), "")

    def test_parse_timestamp_supports_graph_fractional_iso(self):
        ts = parse_timestamp("2026-05-28T12:34:56.123Z")
        self.assertGreater(ts, 1_000_000_000_000)


if __name__ == "__main__":
    unittest.main()
