import base64
import io
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

import httpx
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import padding
from dotenv import load_dotenv

load_dotenv()

MPESA_ENV = os.getenv("MPESA_ENV", "sandbox")
MPESA_CONSUMER_KEY = os.getenv("MPESA_CONSUMER_KEY")
MPESA_CONSUMER_SECRET = os.getenv("MPESA_CONSUMER_SECRET")
MPESA_SHORTCODE = os.getenv("MPESA_SHORTCODE")
MPESA_B2C_SHORTCODE = os.getenv("MPESA_B2C_SHORTCODE") or MPESA_SHORTCODE
MPESA_PASSKEY = os.getenv("MPESA_PASSKEY")
MPESA_CALLBACK_BASE_URL = os.getenv("MPESA_CALLBACK_BASE_URL")
MPESA_INITIATOR_NAME = os.getenv("MPESA_INITIATOR_NAME")
MPESA_INITIATOR_PASSWORD = os.getenv("MPESA_INITIATOR_PASSWORD")
MPESA_CERTIFICATE_PATH = os.getenv("MPESA_CERTIFICATE_PATH")
MPESA_CALLBACK_SECRET = os.getenv("MPESA_CALLBACK_SECRET")
USSD_CALLBACK_SECRET = os.getenv("USSD_CALLBACK_SECRET")

# PayBill vs Till decides the STK TransactionType. Safaricom accepts the wrong
# one and fails downstream, so refuse to start rather than discover it later.
_VALID_MPESA_TRANSACTION_TYPES = {"CustomerPayBillOnline", "CustomerBuyGoodsOnline"}
MPESA_TRANSACTION_TYPE = os.getenv("MPESA_TRANSACTION_TYPE", "CustomerPayBillOnline")
if MPESA_TRANSACTION_TYPE not in _VALID_MPESA_TRANSACTION_TYPES:
    raise RuntimeError(
        f"MPESA_TRANSACTION_TYPE={MPESA_TRANSACTION_TYPE!r} is invalid; must be one of "
        f"{sorted(_VALID_MPESA_TRANSACTION_TYPES)} depending on whether MPESA_SHORTCODE "
        f"is a PayBill (CustomerPayBillOnline) or a Till Number (CustomerBuyGoodsOnline)."
    )

QR_TOKEN_VALIDITY_HOURS = 3

_BASE_URL = "https://sandbox.safaricom.co.ke" if MPESA_ENV == "sandbox" else "https://api.safaricom.co.ke"
_VALID_B2B_RECEIVER_TYPES = {"TILL": "BusinessBuyGoods", "PAYBILL": "BusinessPayBill"}

_cached_token: Optional[str] = None
_cached_token_expires_at: Optional[datetime] = None
_TOKEN_CACHE_TTL_SECONDS = 3300  # Safaricom tokens live ~60 min; refresh early


class MpesaRejected(Exception):
    """Safaricom answered 200 with a non-zero ResponseCode: definitely not queued.

    Safe to reverse a provisional ledger reservation. Never confuse with a timeout.
    """


class MpesaOutcomeUnknown(Exception):
    """HTTP 5xx/408 on a money-moving request: it may or may not have been queued.

    Never reverse the reservation, never mark failed, never retry automatically.
    """


def raise_for_definite_status(response: httpx.Response) -> None:
    """raise_for_status() for money-moving calls: 5xx/408 mean unknown, only other 4xx are rejections."""
    if response.status_code >= 500 or response.status_code == 408:
        raise MpesaOutcomeUnknown(
            f"HTTP {response.status_code} from Safaricom; the outcome of the request is unknown"
        )
    response.raise_for_status()


def _raise_if_rejected(reply: Dict[str, Any], label: str, with_customer_message: bool = False) -> None:
    response_code = reply.get("ResponseCode")
    if response_code is None or str(response_code) == "0":
        return
    detail = (
        f"{label} rejected by Safaricom: ResponseCode={response_code!r} "
        f"ResponseDescription={reply.get('ResponseDescription')!r}"
    )
    if with_customer_message:
        detail += f" CustomerMessage={reply.get('CustomerMessage')!r}"
    raise MpesaRejected(detail)


def _require_initiator_config() -> None:
    if not MPESA_INITIATOR_NAME:
        raise RuntimeError("MPESA_INITIATOR_NAME must be set.")
    if not MPESA_CALLBACK_BASE_URL:
        raise RuntimeError("MPESA_CALLBACK_BASE_URL must be set.")


def _stk_password() -> Tuple[str, str]:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    password = base64.b64encode(f"{MPESA_SHORTCODE}{MPESA_PASSKEY}{timestamp}".encode("utf-8")).decode("utf-8")
    return password, timestamp


def callback_url(endpoint: str) -> str:
    """Provider-facing callback URL. Carries no secret; the gateway injects X-Mpesa-Secret."""
    base = (MPESA_CALLBACK_BASE_URL or "").rstrip("/")
    return f"{base}/callbacks/{endpoint}"


def mask_phone(phone: Optional[str]) -> str:
    """Mask a phone number for logs and alerts, e.g. 2547****5678."""
    if not phone:
        return "unknown"
    digits = str(phone)
    if len(digits) <= 8:
        return "*" * len(digits)
    return digits[:4] + "*" * (len(digits) - 8) + digits[-4:]


def normalize_phone_number(phone: str) -> Optional[str]:
    """Normalize a Kenyan number to 2547XXXXXXXX / 2541XXXXXXXX, or None."""
    digits = re.sub(r"\D", "", phone)
    if len(digits) == 10 and digits.startswith("0"):
        return f"254{digits[1:]}"
    if len(digits) == 9 and digits[0] in "71":
        return f"254{digits}"
    if len(digits) == 12 and digits.startswith("254"):
        return digits
    return None


async def get_access_token(client: httpx.AsyncClient) -> str:
    """Daraja OAuth token, cached in-process for a fixed TTL."""
    global _cached_token, _cached_token_expires_at
    if not MPESA_CONSUMER_KEY or not MPESA_CONSUMER_SECRET:
        raise RuntimeError("MPESA_CONSUMER_KEY and MPESA_CONSUMER_SECRET must be set.")

    now = datetime.now(timezone.utc)
    if _cached_token and _cached_token_expires_at and now < _cached_token_expires_at:
        return _cached_token

    response = await client.get(
        f"{_BASE_URL}/oauth/v1/generate?grant_type=client_credentials",
        auth=(MPESA_CONSUMER_KEY, MPESA_CONSUMER_SECRET),
    )
    response.raise_for_status()
    _cached_token = response.json()["access_token"]
    _cached_token_expires_at = now + timedelta(seconds=_TOKEN_CACHE_TTL_SECONDS)
    return _cached_token


def generate_security_credential() -> str:
    """Initiator password encrypted with Safaricom's public certificate."""
    sandbox_override = os.getenv("MPESA_SECURITY_CREDENTIAL")
    if sandbox_override and MPESA_ENV == "sandbox":
        return sandbox_override.strip()
    if not MPESA_INITIATOR_PASSWORD:
        raise RuntimeError("MPESA_INITIATOR_PASSWORD must be set.")
    if not MPESA_CERTIFICATE_PATH:
        raise RuntimeError(
            "MPESA_CERTIFICATE_PATH is required for B2C payouts. "
            "Mount the Safaricom public certificate into the container and set "
            "MPESA_CERTIFICATE_PATH to that read-only path."
        )
    certificate_path = os.path.abspath(MPESA_CERTIFICATE_PATH)
    if not os.path.isfile(certificate_path):
        raise RuntimeError(
            f"Safaricom B2C certificate not found at {certificate_path!r}. "
            "For Docker, mount ./certs/SafaricomCertificate.cer to "
            "/run/secrets/mpesa/SafaricomCertificate.cer and set "
            "MPESA_CERTIFICATE_PATH=/run/secrets/mpesa/SafaricomCertificate.cer."
        )
    with open(certificate_path, "rb") as cert_file:
        certificate = x509.load_der_x509_certificate(cert_file.read())
    encrypted = certificate.public_key().encrypt(MPESA_INITIATOR_PASSWORD.encode("utf-8"), padding.PKCS1v15())
    return base64.b64encode(encrypted).decode("utf-8")


async def initiate_stk_push(
        client: httpx.AsyncClient,
        phone_number: str,
        amount: int,
        account_reference: str,
        transaction_desc: str,
) -> Dict[str, Any]:
    """M-Pesa Express request to collect funds from the buyer."""
    if not MPESA_SHORTCODE or not MPESA_PASSKEY or not MPESA_CALLBACK_BASE_URL:
        raise RuntimeError("M-Pesa STK push environment variables missing.")

    token = await get_access_token(client)
    password, timestamp = _stk_password()
    payload = {
        "BusinessShortCode": MPESA_SHORTCODE,
        "Password": password,
        "Timestamp": timestamp,
        "TransactionType": MPESA_TRANSACTION_TYPE,
        "Amount": amount,
        "PartyA": phone_number,
        "PartyB": MPESA_SHORTCODE,
        "PhoneNumber": phone_number,
        "CallBackURL": callback_url("stk-callback"),
        "AccountReference": account_reference[:12],
        "TransactionDesc": transaction_desc[:100],
    }
    response = await client.post(
        f"{_BASE_URL}/mpesa/stkpush/v1/processrequest",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
    )
    raise_for_definite_status(response)
    reply = response.json()
    _raise_if_rejected(reply, "STK push", with_customer_message=True)
    return reply


async def initiate_b2c_payout(
        client: httpx.AsyncClient,
        phone_number: str,
        amount: int,
        remarks: str,
        originator_conversation_id: str,
        occasion: str = "",
) -> Dict[str, Any]:
    """B2C payout to a rider (release) or buyer (refund).

    originator_conversation_id must be unique per attempt; Safaricom uses it to
    reject duplicate disbursements.
    """
    _require_initiator_config()
    if not originator_conversation_id:
        raise RuntimeError("originator_conversation_id is required for a B2C payout request.")

    token = await get_access_token(client)
    payload = {
        "OriginatorConversationID": originator_conversation_id,
        "InitiatorName": MPESA_INITIATOR_NAME,
        "SecurityCredential": generate_security_credential(),
        "CommandID": "BusinessPayment",
        "Amount": amount,
        "PartyA": MPESA_B2C_SHORTCODE,
        "PartyB": phone_number,
        "Remarks": remarks[:100],
        "QueueTimeOutURL": callback_url("b2c-timeout"),
        "ResultURL": callback_url("b2c-callback"),
        "Occasion": occasion[:100],
    }
    response = await client.post(
        f"{_BASE_URL}/mpesa/b2c/v1/paymentrequest",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
    )
    raise_for_definite_status(response)
    reply = response.json()
    _raise_if_rejected(reply, "B2C payout")
    return reply


async def register_c2b_urls(client: httpx.AsyncClient) -> Dict[str, Any]:
    """One-off: register the C2B validation/confirmation URLs. Run via scripts/register_c2b_urls.py."""
    if not MPESA_SHORTCODE:
        raise RuntimeError("MPESA_SHORTCODE must be set.")
    if not MPESA_CALLBACK_BASE_URL:
        raise RuntimeError("MPESA_CALLBACK_BASE_URL must be set.")

    token = await get_access_token(client)
    payload = {
        "ShortCode": MPESA_SHORTCODE,
        "ResponseType": "Completed",  # validation URL unreachable: accept the payment rather than lose it
        "ConfirmationURL": callback_url("c2b-confirmation"),
        "ValidationURL": callback_url("c2b-validation"),
    }
    response = await client.post(
        f"{_BASE_URL}/mpesa/c2b/v2/registerurl",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
    )
    response.raise_for_status()
    reply = response.json()
    _raise_if_rejected(reply, "C2B URL registration")
    return reply


def parse_c2b_notification(body: Dict[str, Any]) -> Dict[str, Any]:
    """Fields shared by C2B validation and confirmation. Only validation can reject."""
    return {
        "transaction_type": body.get("TransactionType"),
        "mpesa_receipt_number": body.get("TransID"),
        "transaction_time": body.get("TransTime"),
        "amount": body.get("TransAmount"),
        "business_short_code": body.get("BusinessShortCode"),
        "bill_ref_number": (body.get("BillRefNumber") or "").strip(),
        "msisdn": body.get("MSISDN"),
        "first_name": body.get("FirstName"),
    }


async def initiate_reversal(
        client: httpx.AsyncClient,
        transaction_id: str,
        amount: int,
        remarks: str,
        originator_conversation_id: str,
        occasion: str = "",
) -> Dict[str, Any]:
    """Reverse a settled STK/C2B transaction to the payer. Used for refunds inside the reversal window."""
    _require_initiator_config()
    if not transaction_id:
        raise RuntimeError("transaction_id (the original M-Pesa receipt number) is required for a reversal.")

    token = await get_access_token(client)
    payload = {
        "Initiator": MPESA_INITIATOR_NAME,
        "SecurityCredential": generate_security_credential(),
        "CommandID": "TransactionReversal",
        "TransactionID": transaction_id,
        "Amount": amount,
        "ReceiverParty": MPESA_SHORTCODE,
        "RecieverIdentifierType": "11",  # Daraja: 11 = organization shortcode
        "ResultURL": callback_url("reversal-callback"),
        "QueueTimeOutURL": callback_url("reversal-timeout"),
        "Remarks": remarks[:100],
        "Occasion": occasion[:100],
        "OriginatorConversationID": originator_conversation_id,
    }
    response = await client.post(
        f"{_BASE_URL}/mpesa/reversal/v1/request",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
    )
    raise_for_definite_status(response)
    reply = response.json()
    _raise_if_rejected(reply, "Reversal")
    return reply


def parse_reversal_callback(body: Dict[str, Any]) -> Dict[str, Any]:
    return parse_payout_result_callback(body)


async def initiate_b2b_payout(
        client: httpx.AsyncClient,
        receiver_shortcode: str,
        receiver_channel: str,
        amount: int,
        remarks: str,
        originator_conversation_id: str,
        account_reference: str = "",
) -> Dict[str, Any]:
    """Pay a seller's own Till/Paybill directly instead of via B2C."""
    if receiver_channel not in _VALID_B2B_RECEIVER_TYPES:
        raise RuntimeError(f"receiver_channel must be one of {sorted(_VALID_B2B_RECEIVER_TYPES)}")
    _require_initiator_config()
    if not receiver_shortcode:
        raise RuntimeError("receiver_shortcode (the seller's till/paybill number) is required for a B2B payout.")

    token = await get_access_token(client)
    payload = {
        "Initiator": MPESA_INITIATOR_NAME,
        "SecurityCredential": generate_security_credential(),
        "CommandID": _VALID_B2B_RECEIVER_TYPES[receiver_channel],
        "SenderIdentifierType": "4",  # 4 = organization shortcode
        "RecieverIdentifierType": "4" if receiver_channel == "PAYBILL" else "2",  # 2 = till number
        "Amount": amount,
        "PartyA": MPESA_B2C_SHORTCODE,
        "PartyB": receiver_shortcode,
        "AccountReference": account_reference[:12],
        "Remarks": remarks[:100],
        "QueueTimeOutURL": callback_url("b2b-timeout"),
        "ResultURL": callback_url("b2b-callback"),
        "OriginatorConversationID": originator_conversation_id,
    }
    response = await client.post(
        f"{_BASE_URL}/mpesa/b2b/v1/paymentrequest",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
    )
    raise_for_definite_status(response)
    reply = response.json()
    _raise_if_rejected(reply, "B2B payout")
    return reply


async def query_account_balance(client: httpx.AsyncClient, originator_conversation_id: str) -> Dict[str, Any]:
    """Request the float balance. The figure arrives later on the balance ResultURL."""
    _require_initiator_config()

    token = await get_access_token(client)
    payload = {
        "Initiator": MPESA_INITIATOR_NAME,
        "SecurityCredential": generate_security_credential(),
        "CommandID": "AccountBalance",
        "PartyA": MPESA_B2C_SHORTCODE,
        "IdentifierType": "4",
        "Remarks": "Scheduled float check",
        "QueueTimeOutURL": callback_url("balance-timeout"),
        "ResultURL": callback_url("balance-callback"),
        "OriginatorConversationID": originator_conversation_id,
    }
    response = await client.post(
        f"{_BASE_URL}/mpesa/accountbalance/v1/query",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
    )
    response.raise_for_status()
    reply = response.json()
    _raise_if_rejected(reply, "Account balance query")
    return reply


def _working_account_balance(balance_blob: str) -> Optional[float]:
    """Pick the Working Account figure from "Working Account|KES|1000.00|...&Utility Account|..."."""
    for account in balance_blob.split("&"):
        fields = account.split("|")
        if len(fields) >= 3 and fields[0].strip().lower() == "working account":
            try:
                return float(fields[2])
            except ValueError:
                return None
    return None


def parse_account_balance_callback(body: Dict[str, Any]) -> Dict[str, Any]:
    result = body.get("Result", {})
    result_code = result.get("ResultCode")
    working_balance = None
    if result_code == 0:
        params = result.get("ResultParameters", {}).get("ResultParameter", [])
        param_map = {param.get("Key"): param.get("Value") for param in params}
        working_balance = _working_account_balance(param_map.get("AccountBalance") or "")
    return {
        "conversation_id": result.get("ConversationID"),
        "originator_conversation_id": result.get("OriginatorConversationID"),
        "result_code": result_code,
        "result_desc": result.get("ResultDesc"),
        "success": result_code == 0,
        "working_account_balance": working_balance,
    }


async def query_transaction_status(
        client: httpx.AsyncClient,
        transaction_id: str,
        originator_conversation_id: str,
        occasion: str = "",
) -> Dict[str, Any]:
    """Ask Safaricom for a transaction's real state instead of waiting on a callback."""
    _require_initiator_config()
    if not transaction_id:
        raise RuntimeError("transaction_id is required for a transaction status query.")

    token = await get_access_token(client)
    payload = {
        "Initiator": MPESA_INITIATOR_NAME,
        "SecurityCredential": generate_security_credential(),
        "CommandID": "TransactionStatusQuery",
        "TransactionID": transaction_id,
        "PartyA": MPESA_B2C_SHORTCODE,
        "IdentifierType": "4",
        "ResultURL": callback_url("transaction-status-callback"),
        "QueueTimeOutURL": callback_url("transaction-status-timeout"),
        "Remarks": "Reconciliation query",
        "Occasion": occasion[:100],
        "OriginatorConversationID": originator_conversation_id,
    }
    response = await client.post(
        f"{_BASE_URL}/mpesa/transactionstatus/v1/query",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
    )
    response.raise_for_status()
    reply = response.json()
    _raise_if_rejected(reply, "Transaction status query")
    return reply


def parse_transaction_status_callback(body: Dict[str, Any]) -> Dict[str, Any]:
    result = body.get("Result", {})
    result_code = result.get("ResultCode")
    param_map = {}
    if result_code == 0:
        params = result.get("ResultParameters", {}).get("ResultParameter", [])
        param_map = {param.get("Key"): param.get("Value") for param in params}
    return {
        "conversation_id": result.get("ConversationID"),
        "originator_conversation_id": result.get("OriginatorConversationID"),
        "result_code": result_code,
        "result_desc": result.get("ResultDesc"),
        "success": result_code == 0,
        "transaction_status": param_map.get("TransactionStatus"),
        "receipt_number": param_map.get("ReceiptNo"),
    }


def _stk_query_outcome(status_code: int, body: Any) -> Dict[str, Any]:
    """PAID/FAILED only for a clean 200 with ResponseCode 0 and a readable ResultCode.

    Anything else, including 4999 (still processing), is UNKNOWN: a failed query
    says nothing about whether the customer paid.
    """
    if not isinstance(body, dict):
        body = {}
    result_code = _to_int_or_none(body.get("ResultCode"))
    accepted = status_code == 200 and _to_int_or_none(body.get("ResponseCode")) == 0
    outcome = "UNKNOWN"
    if accepted and result_code is not None and result_code != 4999:
        outcome = "PAID" if result_code == 0 else "FAILED"
    return {
        "checkout_request_id": body.get("CheckoutRequestID"),
        "result_code": result_code,
        "result_desc": body.get("ResultDesc"),
        "outcome": outcome,
    }


async def query_stk_status(client: httpx.AsyncClient, checkout_request_id: str) -> Dict[str, Any]:
    """Ask Safaricom about an STK push whose callback never arrived.

    The answer has no receipt, amount or phone, so PAID is never applied
    automatically. Transport errors propagate; callers treat them as UNKNOWN.
    """
    if not MPESA_SHORTCODE or not MPESA_PASSKEY:
        raise RuntimeError("M-Pesa STK query environment variables missing.")
    if not checkout_request_id:
        raise RuntimeError("checkout_request_id is required for an STK status query.")

    token = await get_access_token(client)
    password, timestamp = _stk_password()
    payload = {
        "BusinessShortCode": MPESA_SHORTCODE,
        "Password": password,
        "Timestamp": timestamp,
        "CheckoutRequestID": checkout_request_id,
    }
    response = await client.post(
        f"{_BASE_URL}/mpesa/stkpushquery/v1/query",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        body = response.json()
    except ValueError:
        body = {}
    return _stk_query_outcome(response.status_code, body)


def _to_int_or_none(value: Any) -> Optional[int]:
    """Read a result code that may arrive as 0, "0" or 0.0. None means unreadable, never failure."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _to_float_or_none(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _name_value_map(items: Any, key_name: str) -> Dict[Any, Any]:
    """[{key_name: k, "Value": v}] -> {k: v}. Daraja sometimes sends a bare object instead of a list."""
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list):
        return {}
    return {item.get(key_name): item.get("Value") for item in items if isinstance(item, dict)}


def parse_stk_callback(body: Dict[str, Any]) -> Dict[str, Any]:
    callback = (body.get("Body") or {}).get("stkCallback") or {}
    result_code = _to_int_or_none(callback.get("ResultCode"))
    metadata = {}
    if result_code == 0:
        metadata = _name_value_map((callback.get("CallbackMetadata") or {}).get("Item", []), "Name")
    return {
        "checkout_request_id": callback.get("CheckoutRequestID"),
        "merchant_request_id": callback.get("MerchantRequestID"),
        "result_code": result_code,
        "result_desc": callback.get("ResultDesc"),
        "success": result_code == 0,
        "receipt_number": metadata.get("MpesaReceiptNumber"),
        "amount": metadata.get("Amount"),
        "phone_number": metadata.get("PhoneNumber"),
    }


def parse_payout_result_callback(body: Dict[str, Any]) -> Dict[str, Any]:
    """Result envelope shared by B2C, Reversal and B2B.

    outcome is SUCCESS, FAILURE or UNKNOWN. An unreadable code is UNKNOWN, never
    FAILURE: the money may have moved.
    """
    result = body.get("Result") or {}
    result_code = _to_int_or_none(result.get("ResultCode"))
    if result_code is None:
        outcome = "UNKNOWN"
    elif result_code == 0:
        outcome = "SUCCESS"
    else:
        outcome = "FAILURE"

    transaction_id = None
    completed_amount = None
    receiver_public_name = None
    if result_code == 0:
        params = _name_value_map((result.get("ResultParameters") or {}).get("ResultParameter", []), "Key")
        transaction_id = result.get("TransactionID") or params.get("TransactionReceipt") or params.get("TransactionID")
        if transaction_id is not None:
            transaction_id = str(transaction_id)
        completed_amount = _to_float_or_none(params.get("TransactionAmount"))
        receiver_public_name = params.get("ReceiverPartyPublicName")

    return {
        "conversation_id": result.get("ConversationID"),
        "originator_conversation_id": result.get("OriginatorConversationID"),
        "result_code": result_code,
        "result_desc": result.get("ResultDesc"),
        "success": outcome == "SUCCESS",
        "outcome": outcome,
        "transaction_id": transaction_id,
        "completed_amount": completed_amount,
        "receiver_public_name": receiver_public_name,
    }


def generate_qr_token() -> str:
    return secrets.token_urlsafe(32)


def qr_token_expiry() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=QR_TOKEN_VALIDITY_HOURS)


def generate_qr_code_png(data: str) -> bytes:
    import qrcode

    buffer = io.BytesIO()
    qrcode.make(data).save(buffer, format="PNG")
    return buffer.getvalue()


def verify_ussd_secret(value: Optional[str]) -> bool:
    """Fails closed: an unset secret refuses everyone, since USSD can cancel orders and trigger payment prompts."""
    if not USSD_CALLBACK_SECRET or not value:
        return False
    return secrets.compare_digest(value.encode("utf-8"), USSD_CALLBACK_SECRET.encode("utf-8"))
