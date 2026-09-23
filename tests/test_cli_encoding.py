"""Redirected CLI output must support Chinese on non-Chinese Windows locales."""
import io
import unittest
from unittest import mock

from gtnhmod import cli


class TestCliEncoding(unittest.TestCase):
    def test_redirected_cp1252_output_uses_utf8(self):
        raw = io.BytesIO()
        stream = io.TextIOWrapper(raw, encoding="cp1252", write_through=True)

        with mock.patch.object(cli.sys, "stdout", stream):
            cli._configure_redirected_output()
            print("刷新 Wiki 数据")

        self.assertEqual(raw.getvalue().decode("utf-8").strip(), "刷新 Wiki 数据")


if __name__ == "__main__":
    unittest.main()
