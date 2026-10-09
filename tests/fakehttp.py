"""Scripted stand-in for urllib's opener: answers per URL, records requests."""

import io
import json
import urllib.error
import urllib.parse


class _Response:
    def __init__(self, status, body):
        self.status = status
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def reply(status, body):
    """An answer: dict bodies are sent as JSON, bytes as they are."""
    return status, json.dumps(body).encode() if isinstance(body, dict) else body


class FakeOpener:
    def __init__(self, script=None):
        # url -> list of answers; each answer is (status, bytes) or an exception to raise.
        self.script = {url: list(answers) for url, answers in (script or {}).items()}
        self.requests = []  # (url, form fields, timeout)

    def open(self, request, timeout=None):
        fields = dict(urllib.parse.parse_qsl(request.data.decode("ascii")))
        self.requests.append((request.full_url, fields, timeout))
        answers = self.script.get(request.full_url)
        if not answers:
            raise AssertionError(f"unexpected request to {request.full_url}")
        answer = answers.pop(0) if len(answers) > 1 else answers[0]
        if isinstance(answer, BaseException):
            raise answer
        status, body = answer
        if status >= 400:
            raise urllib.error.HTTPError(request.full_url, status, "error", {}, io.BytesIO(body))
        return _Response(status, body)

    def fields(self, url):
        return [f for u, f, _ in self.requests if u == url]
