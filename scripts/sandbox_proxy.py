"""SANDBOX ONLY. Forward Safaricom callbacks to the app with the X-Mpesa-Secret header Safaricom can't send,
and save every raw body to sandbox_captures/ so real payload shapes can be inspected.

It adds the secret to anything that reaches it: never expose it on a long-lived or guessable URL, and never use
it in production (that needs a real reverse proxy with an IP allowlist). Only POSTs to /callbacks/* are forwarded.

    uvicorn scripts.sandbox_proxy:app --port 9000
"""
import os
import re
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, Response

load_dotenv()
APP_URL = os.getenv("SANDBOX_APP_URL", "http://127.0.0.1:8000")
SECRET = os.getenv("MPESA_CALLBACK_SECRET", "")
CAPTURE_DIR = Path("sandbox_captures")
CAPTURE_DIR.mkdir(exist_ok=True)
PHONE = re.compile(r"254\d{9}")

app = FastAPI()


def mask(text: str) -> str:
    return PHONE.sub(lambda match: match.group(0)[:6] + "***" + match.group(0)[-3:], text)


@app.post("/{path:path}")
async def forward(path: str, request: Request):
    if not path.startswith("callbacks/") or not SECRET:
        raise HTTPException(404, "not found")
    body = await request.body()
    text = body.decode("utf-8", "replace")
    capture = f"{time.strftime('%Y%m%d-%H%M%S')}_{path.replace('/', '_')}"
    (CAPTURE_DIR / f"{capture}.raw.txt").write_text(text, encoding="utf-8")
    (CAPTURE_DIR / f"{capture}.masked.txt").write_text(mask(text), encoding="utf-8")
    async with httpx.AsyncClient(timeout=30) as client:
        upstream = await client.post(
            f"{APP_URL}/{path}",
            content=body,
            headers={"Content-Type": "application/json", "X-Mpesa-Secret": SECRET},
        )
    (CAPTURE_DIR / f"{capture}.response.txt").write_text(f"{upstream.status_code}\n{mask(upstream.text)}", encoding="utf-8")
    return Response(content=upstream.content, status_code=upstream.status_code, media_type="application/json")
