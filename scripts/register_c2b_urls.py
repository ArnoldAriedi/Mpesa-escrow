"""One-off: register the C2B Validation and Confirmation URLs with Safaricom for MPESA_SHORTCODE.

Safaricom remembers the registration. Run it once per shortcode, and again whenever
MPESA_CALLBACK_BASE_URL changes. Reads the app's MPESA_* environment variables.

    python -m scripts.register_c2b_urls
"""
import asyncio
import sys

import httpx

import escrow_core as mpesa


async def main() -> int:
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            result = await mpesa.register_c2b_urls(client)
        except Exception as exc:
            print(f"C2B URL registration FAILED: {exc}", file=sys.stderr)
            return 1
    print("C2B URL registration accepted by Safaricom:")
    print(f"  ConfirmationURL: {mpesa.callback_url('c2b-confirmation')}")
    print(f"  ValidationURL:   {mpesa.callback_url('c2b-validation')}")
    print(f"  Response: {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
