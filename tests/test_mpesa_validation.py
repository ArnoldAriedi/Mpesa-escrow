from decimal import Decimal

import pytest

import escrow_core as mpesa


def test_stk_callback_parser_extracts_payment_metadata():
    body = {
        "Body": {
            "stkCallback": {
                "MerchantRequestID": "MR-1",
                "CheckoutRequestID": "CR-1",
                "ResultCode": 0,
                "ResultDesc": "Success",
                "CallbackMetadata": {
                    "Item": [
                        {"Name": "Amount", "Value": 1100},
                        {"Name": "MpesaReceiptNumber", "Value": "ABC123"},
                        {"Name": "PhoneNumber", "Value": 254712345678},
                    ]
                },
            }
        }
    }
    parsed = mpesa.parse_stk_callback(body)
    assert parsed["success"] is True
    assert parsed["receipt_number"] == "ABC123"
    assert Decimal(str(parsed["amount"])) == Decimal("1100")
    assert parsed["phone_number"] == 254712345678


def test_phone_normalization():
    assert mpesa.normalize_phone_number("0712 345 678") == "254712345678"
    assert mpesa.normalize_phone_number("+254712345678") == "254712345678"
    assert mpesa.normalize_phone_number("invalid") is None
