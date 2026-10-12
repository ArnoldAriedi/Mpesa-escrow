import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from escrow_core import mask_phone
from escrow_models import Notification, NotificationChannel, NotificationStatus

# SMS_PROVIDER: "africastalking", "generic", or unset. Unset uses the generic
# webhook if NOTIFICATION_PROVIDER_URL is set, otherwise prints (dev only).
SMS_PROVIDER = os.getenv("SMS_PROVIDER", "").strip().lower()
SMS_MAX_ATTEMPTS = int(os.getenv("SMS_MAX_ATTEMPTS", "3"))
SMS_RETRY_BACKOFF_SECONDS = float(os.getenv("SMS_RETRY_BACKOFF_SECONDS", "1"))
NOTIFICATION_MAX_TOTAL_ATTEMPTS = int(os.getenv("NOTIFICATION_MAX_TOTAL_ATTEMPTS", "10"))

AFRICASTALKING_USERNAME = os.getenv("AFRICASTALKING_USERNAME")
AFRICASTALKING_API_KEY = os.getenv("AFRICASTALKING_API_KEY")
AFRICASTALKING_SENDER_ID = os.getenv("AFRICASTALKING_SENDER_ID")
AFRICASTALKING_ENV = os.getenv("AFRICASTALKING_ENV", "production").strip().lower()
AFRICASTALKING_BASE_URL = (
    "https://api.sandbox.africastalking.com/version1/messaging"
    if AFRICASTALKING_ENV == "sandbox"
    else "https://api.africastalking.com/version1/messaging"
)
# 100/101/102 = processed/sent/queued. Anything else is a rejection even on HTTP 200.
_AFRICASTALKING_ACCEPTED_STATUS_CODES = {100, 101, 102}

NOTIFICATION_PROVIDER_URL = os.getenv("NOTIFICATION_PROVIDER_URL")
NOTIFICATION_PROVIDER_TOKEN = os.getenv("NOTIFICATION_PROVIDER_TOKEN")


class SmsSendError(Exception):
    """Provider confirmed it rejected the message."""


def _to_e164(phone: str) -> str:
    """Numbers are stored as bare 2547...; Africa's Talking wants a leading +."""
    return phone if phone.startswith("+") else f"+{phone}"


async def _send_via_africastalking(client: httpx.AsyncClient, phone: str, message: str) -> str:
    if not AFRICASTALKING_USERNAME or not AFRICASTALKING_API_KEY:
        raise SmsSendError("AFRICASTALKING_USERNAME/AFRICASTALKING_API_KEY are not configured")
    form = {"username": AFRICASTALKING_USERNAME, "to": _to_e164(phone), "message": message}
    if AFRICASTALKING_SENDER_ID:
        form["from"] = AFRICASTALKING_SENDER_ID
    headers = {
        "apiKey": AFRICASTALKING_API_KEY,
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
    }
    response = await client.post(AFRICASTALKING_BASE_URL, data=form, headers=headers)
    response.raise_for_status()
    body = response.json()
    recipients = (body.get("SMSMessageData") or {}).get("Recipients") or []
    if not recipients:
        raise SmsSendError(f"Africa's Talking returned no recipients in the response: {body}")
    first = recipients[0]
    status_code = first.get("statusCode")
    if status_code not in _AFRICASTALKING_ACCEPTED_STATUS_CODES:
        raise SmsSendError(
            f"Africa's Talking rejected the message (status={first.get('status')!r}, "
            f"statusCode={status_code})"
        )
    return str(first.get("messageId") or "")


async def _send_via_generic_webhook(client: httpx.AsyncClient, phone: str, message: str) -> str:
    if not NOTIFICATION_PROVIDER_URL:
        raise SmsSendError("NOTIFICATION_PROVIDER_URL is not configured")
    headers = {"Authorization": f"Bearer {NOTIFICATION_PROVIDER_TOKEN}"} if NOTIFICATION_PROVIDER_TOKEN else {}
    response = await client.post(NOTIFICATION_PROVIDER_URL, json={"to": phone, "message": message}, headers=headers)
    response.raise_for_status()
    if not response.content:
        return ""
    body = response.json()
    if not body:
        return ""
    return str(body.get("message_id") or body.get("id") or "")


async def _attempt_send(phone: str, message: str) -> Optional[str]:
    """One send attempt; returns the provider message id.

    SmsSendError is a confirmed rejection and must not be retried. Any other
    exception is an unknown outcome and may be.
    """
    if SMS_PROVIDER == "" and not NOTIFICATION_PROVIDER_URL:
        print(f"[SMS DEV] {phone}: {message}")
        return None
    if SMS_PROVIDER not in ("", "africastalking", "generic"):
        raise SmsSendError(f"Unknown SMS_PROVIDER={SMS_PROVIDER!r}; expected 'africastalking' or 'generic'")
    async with httpx.AsyncClient(timeout=15) as client:
        if SMS_PROVIDER == "africastalking":
            return await _send_via_africastalking(client, phone, message)
        return await _send_via_generic_webhook(client, phone, message)


async def send_sms(db: AsyncSession, phone: str, message: str, idempotency_key: str) -> Notification:
    existing = await db.scalar(select(Notification).where(Notification.idempotency_key == idempotency_key))
    if existing:
        return existing
    notification = Notification(
        recipient_phone=phone, channel=NotificationChannel.SMS, message=message, idempotency_key=idempotency_key
    )
    db.add(notification)
    await db.flush()

    last_error: Optional[str] = None
    for attempt in range(1, SMS_MAX_ATTEMPTS + 1):
        notification.attempts = attempt
        try:
            message_id = await _attempt_send(phone, message)
        except SmsSendError as exc:
            last_error = str(exc)
            break
        except Exception as exc:
            last_error = str(exc)
            if attempt < SMS_MAX_ATTEMPTS:
                logging.warning(
                    "SMS send attempt %d/%d failed for %s, retrying: %s",
                    attempt, SMS_MAX_ATTEMPTS, mask_phone(phone), exc,
                )
                await asyncio.sleep(SMS_RETRY_BACKOFF_SECONDS * attempt)
            continue
        notification.provider_message_id = message_id or None
        notification.status = NotificationStatus.SENT
        notification.sent_at = datetime.now(timezone.utc)
        last_error = None
        break

    if last_error is not None:
        notification.status = NotificationStatus.FAILED
        notification.error = last_error
    await db.flush()
    return notification


async def retry_failed_notification(db: AsyncSession, notification: Notification) -> bool:
    """Background-sweep retry of one FAILED notification. True if it was sent.

    Rejections count toward NOTIFICATION_MAX_TOTAL_ATTEMPTS like any failure.
    """
    if notification.status != NotificationStatus.FAILED:
        return False
    if notification.attempts >= NOTIFICATION_MAX_TOTAL_ATTEMPTS:
        return False
    notification.attempts += 1
    try:
        message_id = await _attempt_send(notification.recipient_phone, notification.message)
    except Exception as exc:
        notification.error = str(exc)
        return False
    notification.provider_message_id = message_id or None
    notification.status = NotificationStatus.SENT
    notification.sent_at = datetime.now(timezone.utc)
    notification.error = None
    return True
