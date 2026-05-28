import json
import unittest

import relogin


class FakeResponse:
    def __init__(self, status_code=200, data=None):
        self.status_code = status_code
        self._data = data or {}
        self.text = json.dumps(self._data)

    def json(self):
        return self._data


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.posts = []

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return self.response


class ReloginLoggingTests(unittest.TestCase):
    def test_prefer_one_time_code_login_defaults_true(self):
        old_cfg = relogin.CFG
        try:
            relogin.configure({"http": {"user_agent_chrome": "ua"}, "chatgpt": {}})
            self.assertTrue(relogin.prefer_one_time_code_login())
            relogin.configure({"http": {"user_agent_chrome": "ua"}, "chatgpt": {"prefer_one_time_code": False}})
            self.assertFalse(relogin.prefer_one_time_code_login())
        finally:
            relogin.configure(old_cfg)

    def test_log_to_receives_step_start_and_finish(self):
        logs = []
        timings = []
        with relogin.log_to(logs.append):
            relogin.tick(timings, "Unit Step")
            relogin.tock(timings)

        self.assertIn("开始 Unit Step", logs)
        self.assertTrue(any(line.startswith("完成 Unit Step") for line in logs))

    def test_resend_current_email_otp_posts_resend_and_logs(self):
        session = FakeSession(FakeResponse(200, {"continue_url": "/next", "page": {"type": "email_otp_verification"}}))
        logs = []
        with relogin.log_to(logs.append):
            next_url, page_type = relogin.resend_current_email_otp(session, {"Authorization": "x"})

        self.assertEqual(next_url, "/next")
        self.assertEqual(page_type, "email_otp_verification")
        self.assertEqual(len(session.posts), 1)
        url, kwargs = session.posts[0]
        self.assertTrue(url.endswith("/api/accounts/email-otp/resend"))
        self.assertEqual(kwargs["json"], {})
        self.assertTrue(any("resend" in line for line in logs))


if __name__ == "__main__":
    unittest.main()
