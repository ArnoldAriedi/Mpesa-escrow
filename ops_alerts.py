import logging
import os

import httpx

# Slack-style incoming webhook accepting {"text": "..."}. Unset = log only.
OPS_ALERT_WEBHOOK_URL = os.getenv("OPS_ALERT_WEBHOOK_URL")


async def alert_payout_issue(kind: str, payout_id: int, escrow_transaction_id: int, detail: str) -> None:
    """Flag a payout for human attention. Never raises: a dead alert channel must not block bookkeeping."""
    message = f"[escrow-alert] {kind}: payout #{payout_id} (escrow #{escrow_transaction_id}) — {detail}"
    if not OPS_ALERT_WEBHOOK_URL:
        logging.warning(message)
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(OPS_ALERT_WEBHOOK_URL, json={"text": message})
            response.raise_for_status()
    except Exception as exc:
        logging.error("Failed to deliver ops alert (%s): %s", exc, message)
