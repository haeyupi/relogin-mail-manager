import unittest

from app import normalize_email_list


class AppHelperTests(unittest.TestCase):
    def test_normalize_email_list_deduplicates_and_skips_bad_values(self):
        emails = normalize_email_list([" USER@outlook.com ", "", "user@outlook.com", "other@hotmail.com"])
        self.assertEqual(emails, ["user@outlook.com", "other@hotmail.com"])


if __name__ == "__main__":
    unittest.main()
