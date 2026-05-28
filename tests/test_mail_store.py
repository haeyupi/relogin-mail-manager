import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from mail_store import MailStore


class MailStoreImportZipTests(unittest.TestCase):
    def test_import_zip_with_root_prefix_and_remote_hints(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bundle = tmp_path / "bundle.zip"
            with zipfile.ZipFile(bundle, "w") as zf:
                zf.writestr(
                    "export/mail/mail_accounts.txt",
                    "\n".join(
                        [
                            "user@hotmail.com----mailpw----client-id----refresh-token",
                            "bad-line",
                            "other@hotmail.com----mailpw2----client-id2----refresh-token2",
                        ]
                    ),
                )
                zf.writestr("export/mail/gpt_passwords.txt", "user@hotmail.com----gpt_gpt-password")
                zf.writestr("export/mail/phone_numbers.txt", "user@hotmail.com----+15550001111")
                zf.writestr("export/cpa/auth_user.json", json.dumps({"_email": "user@hotmail.com"}))
                zf.writestr(
                    "export/sub2api/accounts.json",
                    json.dumps(
                        {
                            "accounts": [
                                {
                                    "id": 42,
                                    "name": "other-account",
                                    "credentials": {"email": "other@hotmail.com"},
                                }
                            ]
                        }
                    ),
                )

            store = MailStore(tmp_path / "mail.sqlite3")
            try:
                result = store.import_zip(bundle)
                self.assertEqual(result["imported"], 2)
                self.assertEqual(result["failed"], 1)
                user = store.get("USER@hotmail.com")
                self.assertEqual(user["gpt_password"], "gpt-password")
                self.assertEqual(user["phone_number"], "+15550001111")
                self.assertEqual(user["remote_provider"], "cpa")
                self.assertEqual(user["remote_name"], "auth_user.json")
                other = store.get("other@hotmail.com")
                self.assertEqual(other["remote_provider"], "sub2api")
                self.assertEqual(other["remote_id"], "42")
                self.assertEqual(other["remote_name"], "other-account")
            finally:
                store.close()

    def test_import_zip_requires_mail_accounts(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle = Path(tmp) / "bundle.zip"
            with zipfile.ZipFile(bundle, "w") as zf:
                zf.writestr("mail/gpt_passwords.txt", "user@hotmail.com----pw")
            store = MailStore(Path(tmp) / "mail.sqlite3")
            try:
                with self.assertRaisesRegex(RuntimeError, "mail/mail_accounts.txt"):
                    store.import_zip(bundle)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
