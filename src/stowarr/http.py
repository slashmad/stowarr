from __future__ import annotations

import json
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar


class JsonClient:
    def __init__(self, base_url: str, headers: dict[str, str] | None = None):
        self.base_url = base_url.rstrip("/")
        self.headers = headers or {}
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))

    def request(self, method: str, path: str, *, query: dict | None = None, form: dict | None = None, body=None):
        payload = self._request(method, path, query=query, form=form, body=body)
        return json.loads(payload) if payload else None

    def request_text(
        self,
        method: str,
        path: str,
        *,
        query: dict | None = None,
        form: dict | None = None,
        body=None,
    ) -> str:
        payload = self._request(method, path, query=query, form=form, body=body)
        return payload.decode("utf-8").strip() if payload else ""

    def _request(self, method: str, path: str, *, query: dict | None = None, form: dict | None = None, body=None) -> bytes:
        url = f"{self.base_url}{path}"
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = None
        headers = dict(self.headers)
        if form is not None:
            data = urllib.parse.urlencode(form).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        with self.opener.open(request, timeout=60) as response:
            return response.read()
