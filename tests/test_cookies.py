"""cookies.py 解析测试（离线）。Copy as cURL 的两种风格 + 直接粘贴头。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gtnhmod.cookies import parse_curl, parse_paste  # noqa: E402

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36"
COOKIE = "cf_clearance=AbCdEf-_123.456; _ga=GA1.1.1234567890.1700000000"


class TestParseCurl(unittest.TestCase):
    def test_bash_style(self):
        text = (f"curl 'https://gtnh.huijiwiki.com/index.php?title=X&action=raw' \\\n"
                f"  -H 'accept: text/plain' \\\n"
                f"  -H 'cookie: {COOKIE}' \\\n"
                f"  -H 'user-agent: {UA}' \\\n"
                f"  --compressed")
        cookie, ua = parse_curl(text)
        self.assertEqual(cookie, COOKIE)
        self.assertEqual(ua, UA)

    def test_cmd_style_with_caret_continuation(self):
        text = (f'curl.exe "^https://gtnh.huijiwiki.com/index.php" ^\n'
                f'  -H "cookie: {COOKIE}" ^\n'
                f'  -H "user-agent: {UA}" ^\n'
                f'  -H "referer: https://gtnh.huijiwiki.com/wiki/X"')
        cookie, ua = parse_curl(text)
        self.assertEqual(cookie, COOKIE)
        self.assertEqual(ua, UA)

    def test_dedicated_flags(self):
        text = f"curl https://example.com -A '{UA}' -b '{COOKIE}' -sS"
        cookie, ua = parse_curl(text)
        self.assertEqual(cookie, COOKIE)
        self.assertEqual(ua, UA)

    def test_single_line_cmd(self):
        text = f'curl.exe "https://x/" -H "cookie: {COOKIE}" -H "user-agent: {UA}"'
        cookie, ua = parse_curl(text)
        self.assertEqual(cookie, COOKIE)
        self.assertEqual(ua, UA)

    def test_no_cookie_no_ua(self):
        cookie, ua = parse_curl("curl 'https://example.com' --compressed")
        self.assertEqual((cookie, ua), ("", ""))

    def test_empty(self):
        self.assertEqual(parse_curl(""), ("", ""))
        self.assertEqual(parse_curl(None), ("", ""))


class TestParsePaste(unittest.TestCase):
    def test_curl_content(self):
        text = (f"curl 'https://x/' -H 'cookie: {COOKIE}' -H 'user-agent: {UA}'")
        self.assertEqual(parse_paste(text), (COOKIE, UA))

    def test_raw_cookie_header_line(self):
        self.assertEqual(parse_paste(f"Cookie: {COOKIE}")[0], COOKIE)

    def test_cookie_and_ua_lines(self):
        text = f"Cookie: {COOKIE}\nUser-Agent: {UA}"
        self.assertEqual(parse_paste(text), (COOKIE, UA))

    def test_bare_cookie_string(self):
        self.assertEqual(parse_paste("cf_clearance=AbCdEf-_123.456")[0],
                         "cf_clearance=AbCdEf-_123.456")

    def test_ua_alone_is_not_cookie(self):
        cookie, ua = parse_paste(UA)
        self.assertEqual(cookie, "")
        self.assertEqual(ua, "")

    def test_empty(self):
        self.assertEqual(parse_paste(""), ("", ""))
        self.assertEqual(parse_paste(None), ("", ""))
