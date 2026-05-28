import unittest

from providers import RemoteAccount, Sub2ApiProvider, target_config


class ProviderTests(unittest.TestCase):
    def test_target_config_supports_legacy_cpa(self):
        cfg = {
            "target": {"provider": "", "base_url": "", "management_key": ""},
            "cpa": {"enabled": True, "base_url": "http://127.0.0.1:3000", "management_key": "secret"},
        }
        target = target_config(cfg)
        self.assertEqual(target["provider"], "cpa")
        self.assertTrue(target["enabled"])
        self.assertEqual(target["base_url"], "http://127.0.0.1:3000")

    def test_sub2api_list_accounts_paginates_and_extracts_email(self):
        cfg = {"chatgpt": {"codex_client_id": "app"}}
        provider = Sub2ApiProvider(
            cfg,
            {
                "base_url": "http://sub2api.local",
                "management_key": "secret",
                "request_timeout": 1,
                "enabled": True,
                "sub2api_concurrency": 3,
                "sub2api_priority": 50,
            },
        )
        calls = []

        def fake_request(method, endpoint, body=None, query=None):
            calls.append(query["page"])
            if query["page"] == 1:
                return {
                    "data": {
                        "items": [
                            {"id": 1, "name": "a", "credentials": {"email": "a@hotmail.com"}},
                        ],
                        "total": 2,
                        "pages": 2,
                    }
                }
            return {
                "data": {
                    "items": [
                        {"id": 2, "name": "b", "credentials": {"email": "b@hotmail.com"}},
                    ],
                    "total": 2,
                    "pages": 2,
                }
            }

        provider.request_json = fake_request
        remotes = provider.list_accounts()
        self.assertEqual(calls, [1, 2])
        self.assertEqual([r.email for r in remotes], ["a@hotmail.com", "b@hotmail.com"])

    def test_sub2api_upload_creates_when_no_match(self):
        cfg = {"chatgpt": {"codex_client_id": "app_EMoam"}}
        provider = Sub2ApiProvider(
            cfg,
            {
                "base_url": "http://sub2api.local",
                "management_key": "secret",
                "request_timeout": 1,
                "enabled": True,
                "sub2api_concurrency": 3,
                "sub2api_priority": 50,
            },
        )
        provider.list_accounts = lambda: []
        seen = {}

        def fake_request(method, endpoint, body=None, query=None):
            seen["method"] = method
            seen["endpoint"] = endpoint
            seen["body"] = body
            return {"data": {"id": 99, "name": body["name"], "credentials": body["credentials"]}}

        provider.request_json = fake_request
        result = provider.upload_token_result(
            {"email": "new@hotmail.com"},
            {
                "_email": "new@hotmail.com",
                "tokens": {
                    "access_token": "access",
                    "refresh_token": "refresh",
                    "id_token": "id",
                    "account_id": "acct",
                    "expires_at": "2026-05-28T12:00:00Z",
                },
            },
        )
        self.assertEqual(seen["method"], "POST")
        self.assertEqual(seen["endpoint"], "/api/v1/admin/accounts")
        self.assertEqual(seen["body"]["platform"], "openai")
        self.assertEqual(seen["body"]["credentials"]["client_id"], "app_EMoam")
        self.assertEqual(result["remote_id"], "99")

    def test_find_match_uses_remote_id_email_or_name(self):
        provider = Sub2ApiProvider(
            {"chatgpt": {}},
            {
                "base_url": "http://sub2api.local",
                "management_key": "secret",
                "request_timeout": 1,
                "enabled": True,
                "sub2api_concurrency": 3,
                "sub2api_priority": 50,
            },
        )
        remotes = [RemoteAccount("sub2api", email="user@hotmail.com", remote_id="7", name="auth_user")]
        self.assertIsNotNone(provider.find_match({"email": "USER@hotmail.com"}, remotes))
        self.assertIsNotNone(provider.find_match({"email": "x@hotmail.com", "remote_id": "7"}, remotes))
        self.assertIsNotNone(provider.find_match({"email": "x@hotmail.com", "remote_name": "auth_user"}, remotes))


if __name__ == "__main__":
    unittest.main()
