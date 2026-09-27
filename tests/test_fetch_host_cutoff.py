"""A source host that stops answering costs a run two URLs' worth of retries, not all of them.

Found on the real services: CBA's download host stopped completing TLS handshakes while its main
site answered. Every file on it spent its full timeout four times over, and a check that touches
thirty-odd CBA files would have outlasted the runner.
"""
from __future__ import annotations

import requests
import pytest

from azmonitor.ingest.fetch import FetchError, Fetcher


class _Response:
    status_code = 200
    url = "https://ok.example/f.xlsx"
    headers = {"Content-Type": "application/octet-stream"}

    def iter_content(self, chunk_size):
        yield b"PK\x03\x04data"


class _Session:
    def __init__(self, down: set[str]):
        self.down = down
        self.calls: list[str] = []
        self.headers = {}

    def get(self, url, timeout, stream):
        self.calls.append(url)
        if any(h in url for h in self.down):
            raise requests.ConnectionError("TLS handshake timed out")
        return _Response()


def _fetcher(tmp_path, down):
    f = Fetcher({"retries": 1, "backoff_seconds": 0, "min_interval_seconds": 0}, tmp_path)
    f.session = _Session(down)
    return f


def test_after_two_urls_fail_outright_the_rest_of_that_host_fails_at_once(tmp_path):
    f = _fetcher(tmp_path, {"uploads.cbar.az"})
    for n in (1, 2):
        with pytest.raises(FetchError, match="giving up"):
            f.get(f"https://uploads.cbar.az/assets/{n}.xlsx")
    before = len(f.session.calls)
    with pytest.raises(FetchError, match="skipping .* failed 2 requests in a row"):
        f.get("https://uploads.cbar.az/assets/3.xlsx")
    assert len(f.session.calls) == before, "no request is spent on a host already given up on"
    assert f.get("https://www.cbar.az/page").status == 200, "other hosts are unaffected"


def test_a_success_resets_the_count(tmp_path):
    f = _fetcher(tmp_path, {"flaky.example"})
    with pytest.raises(FetchError):
        f.get("https://flaky.example/1")
    f.session.down = set()
    assert f.get("https://flaky.example/2").status == 200
    f.session.down = {"flaky.example"}
    with pytest.raises(FetchError, match="giving up"):
        f.get("https://flaky.example/3")          # one failure since the success, not two
    with pytest.raises(FetchError, match="giving up"):
        f.get("https://flaky.example/4")


def test_a_host_that_answers_with_the_wrong_thing_is_not_counted_as_down(tmp_path):
    f = _fetcher(tmp_path, set())

    class Html(_Response):
        headers = {"Content-Type": "text/html"}

    f.session.get = lambda url, timeout, stream: Html()
    for n in range(4):
        with pytest.raises(FetchError, match="expected a file"):
            f.get(f"https://answers.example/{n}", allow_html=False)
    assert f._host_failures.get("answers.example", 0) == 0
