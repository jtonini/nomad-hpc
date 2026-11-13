# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""The worker's side (1.45.0): ask the Console for a training request, take
it, train, send back the result. Over HTTPS, with the worker's key.

The Console's routes (fix 110), each POST with X-ML-Worker-Key:

    /api/ml/worker/claim             {worker, versions} -> {request: {id, plan} | null}
    /api/ml/worker/{id}/data         -> the dataset (nomad.learn.dataset)
    /api/ml/worker/{id}/progress     {worker, text}
    /api/ml/worker/{id}/result       {worker, summary, scores}
    /api/ml/worker/{id}/fail         {worker, error}
"""
from __future__ import annotations

import gzip
import json
import ssl
import urllib.error
import urllib.request

from nomad.learn.settings import WorkerSettings, read_key

TIMEOUT = 120
DATA_TIMEOUT = 900


class WorkerError(RuntimeError):
    """The Console couldn't be asked, or said no: why, in words."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: urllib would send the worker's key on to wherever it points
    (a sign-in page on another host, say). A redirect is an error, said in words."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Client:
    def __init__(self, ws: WorkerSettings):
        if not ws.console:
            raise WorkerError("[ml.worker] console isn't set: the Console's address (https://...)")
        key = read_key(ws.key_file)
        if key is None:
            raise WorkerError(f"no worker key in {ws.key_file} (32 characters or more): nomad ml key")
        self.ws, self._key = ws, key
        self._ctx = ssl.create_default_context(cafile=ws.ca_file) if ws.console.startswith("https://") else None
        handlers = [_NoRedirect()] + ([urllib.request.HTTPSHandler(context=self._ctx)] if self._ctx else [])
        self._open = urllib.request.build_opener(*handlers).open

    def post(self, path: str, body: dict | None = None, timeout: int = TIMEOUT) -> dict:
        data = json.dumps(body or {}).encode()
        req = urllib.request.Request(self.ws.console + path, data=data, method="POST", headers={
            "Content-Type": "application/json", "Accept-Encoding": "gzip", "X-ML-Worker-Key": self._key,
            "User-Agent": f"nomad-ml-worker/{self.ws.name}"})
        try:
            with self._open(req, timeout=timeout) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
        except urllib.error.HTTPError as e:
            if 300 <= e.code < 400:
                where = (e.headers.get("Location") or "").split("?")[0]
                raise WorkerError(f"{self.ws.console}{path} answered with a redirect ({e.code}"
                                  + (f", to {where}" if where else "") + "), not followed: a sign-in page in "
                                  "front of the Console? Let /api/ml/worker/ through to it, or set [ml.worker] "
                                  "console to the address the Console itself answers at") from None
            try:
                detail = json.loads(e.read().decode() or "{}").get("detail")
            except (ValueError, AttributeError):
                detail = None
            raise WorkerError(f"the Console said {e.code}" + (f": {detail}" if detail else "")) from None
        except (urllib.error.URLError, OSError) as e:
            raise WorkerError(f"couldn't reach the Console at {self.ws.console}: {getattr(e, 'reason', e)}") from None
        try:
            got = json.loads(raw.decode() or "{}")
        except (ValueError, UnicodeDecodeError):
            got = None
        if not isinstance(got, dict):
            raise WorkerError(f"{self.ws.console}{path} didn't answer as the Console does (not JSON): a web page "
                              "in front of it? The Console needs fix 110") from None
        return got


def work_once(ws: WorkerSettings, say=print, client: Client | None = None) -> str:
    """One pass: nothing to do, or one request taken, trained and answered.
    What happened, in a line."""
    from nomad.learn import train
    c = client or Client(ws)
    got = c.post("/api/ml/worker/claim", {"worker": ws.name, "versions": train._versions()})
    req = got.get("request")
    if not req:
        return "no training request"
    rid = int(req["id"])
    say(f"request {rid}: taken")

    def progress(text):
        say(f"request {rid}: {text}")
        try:
            c.post(f"/api/ml/worker/{rid}/progress", {"worker": ws.name, "text": text})
        except WorkerError as e:
            say(f"request {rid}: progress not sent ({e})")
    try:
        progress("downloading the jobs")
        data = c.post(f"/api/ml/worker/{rid}/data", {"worker": ws.name}, timeout=DATA_TIMEOUT)
        progress(f"training on {len(data.get('rows', []))} jobs")
        result = train.run(data, ws.device, progress)
        progress("sending the result")
        c.post(f"/api/ml/worker/{rid}/result", {"worker": ws.name, **result}, timeout=DATA_TIMEOUT)
    except Exception as e:                       # noqa: BLE001 -- said to the Console, then raised
        why = f"{type(e).__name__}: {e}" if not isinstance(e, (WorkerError, train.TrainError)) else str(e)
        try:
            c.post(f"/api/ml/worker/{rid}/fail", {"worker": ws.name, "error": why[:500]})
        except WorkerError:
            pass
        raise
    return f"request {rid}: done"


__all__ = ["Client", "WorkerError", "work_once"]
