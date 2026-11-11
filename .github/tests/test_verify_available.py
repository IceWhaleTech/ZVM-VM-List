import unittest
from unittest.mock import MagicMock, patch

import verify_available as verify


class VerifyTests(unittest.TestCase):
    def check(self, status=206, headers=None, body=b"\0" * 512):
        response = MagicMock()
        response.__enter__.return_value = response
        response.status_code = status
        response.headers = headers if headers is not None else {"Content-Range": "bytes 0-511/4096"}
        response.iter_content.return_value = iter([body])
        with patch.object(verify.requests, "get", return_value=response):
            return verify.check_url({"id": "fedora", "url": "https://example.com/fedora.iso", "size": 4096})

    def test_partial_iso_is_available(self):
        self.assertIsNone(self.check())

    def test_server_ignoring_range_is_available(self):
        self.assertIsNone(self.check(status=200, headers={"Content-Length": "4096"}))

    def test_http_200_bot_challenge_is_unavailable(self):
        self.assertIn("HTML", self.check(status=200, headers={"Content-Type": "text/html; charset=utf-8"},
                                         body=b"<!doctype html><title>Making sure you're not a bot!</title>"))

    def test_html_with_binary_content_type_is_unavailable(self):
        self.assertIn("HTML", self.check(status=200, headers={"Content-Type": "application/octet-stream"},
                                         body=b"  <!DOCTYPE HTML><html>challenge</html>"))

    def test_size_mismatch_is_unavailable(self):
        self.assertIn("size mismatch", self.check(headers={"Content-Range": "bytes 0-511/8192"}))

    def test_empty_response_is_unavailable(self):
        self.assertIn("empty", self.check(body=b""))

    def test_invalid_range_is_unavailable(self):
        self.assertIn("invalid Content-Range", self.check(headers={}))


if __name__ == "__main__":
    unittest.main()
