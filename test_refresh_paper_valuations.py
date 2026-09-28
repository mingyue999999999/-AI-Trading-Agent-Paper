import importlib.util
import io
import json
import urllib.error
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).parent / "scripts" / "refresh_paper_valuations.py"
if not MODULE_PATH.exists():
    MODULE_PATH = Path(__file__).with_name("refresh_paper_valuations.py")
SPEC = importlib.util.spec_from_file_location("refresh_paper_valuations", MODULE_PATH)
valuation = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(valuation)


class Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class GetJsonRetryTests(unittest.TestCase):
    @mock.patch.object(valuation.time, "sleep")
    @mock.patch.object(valuation.urllib.request, "urlopen")
    def test_retries_transient_connection_reset(self, urlopen, sleep):
        urlopen.side_effect = [ConnectionResetError(104, "reset"), Response({"data": [1]})]
        self.assertEqual(valuation.get_json("https://example.test", base_delay=0.25), {"data": [1]})
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once_with(0.25)

    @mock.patch.object(valuation.time, "sleep")
    @mock.patch.object(valuation.urllib.request, "urlopen")
    def test_does_not_retry_permanent_http_error(self, urlopen, sleep):
        urlopen.side_effect = urllib.error.HTTPError("https://example.test", 403, "Forbidden", {}, io.BytesIO())
        with self.assertRaises(urllib.error.HTTPError):
            valuation.get_json("https://example.test")
        self.assertEqual(urlopen.call_count, 1)
        sleep.assert_not_called()

    @mock.patch.object(valuation.time, "sleep")
    @mock.patch.object(valuation.urllib.request, "urlopen")
    def test_stops_after_bounded_attempts(self, urlopen, sleep):
        urlopen.side_effect = ConnectionResetError(104, "reset")
        with self.assertRaises(ConnectionResetError):
            valuation.get_json("https://example.test", attempts=3, base_delay=0.1)
        self.assertEqual(urlopen.call_count, 3)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [0.1, 0.2])


if __name__ == "__main__":
    unittest.main()
