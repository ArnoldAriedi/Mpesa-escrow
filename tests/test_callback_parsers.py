"""Regression tests for N14: Safaricom callback parsers must read results
defensively, and an unreadable result must never look like a failure."""
import pytest

import escrow_core as mpesa


def stk(result_code, items=None):
    cb = {"CheckoutRequestID": "ws_CO_1", "MerchantRequestID": "m1",
          "ResultCode": result_code, "ResultDesc": "x"}
    if items is not None:
        cb["CallbackMetadata"] = {"Item": items}
    return {"Body": {"stkCallback": cb}}


ITEMS = [
    {"Name": "Amount", "Value": 500},
    {"Name": "MpesaReceiptNumber", "Value": "QWE123"},
    {"Name": "PhoneNumber", "Value": 254700000001},
]


@pytest.mark.parametrize("code", [0, "0", " 0 ", 0.0])
def test_stk_success_is_recognised_whatever_the_code_type(code):
    parsed = mpesa.parse_stk_callback(stk(code, ITEMS))
    assert parsed["success"] is True
    assert parsed["receipt_number"] == "QWE123"
    assert parsed["amount"] == 500


@pytest.mark.parametrize("code", [1032, "1032", 1])
def test_stk_failure_is_a_failure(code):
    parsed = mpesa.parse_stk_callback(stk(code))
    assert parsed["success"] is False
    assert parsed["result_code"] == int(code)


@pytest.mark.parametrize("code", [None, "", "abc", True])
def test_stk_unreadable_code_is_neither_success_nor_a_real_code(code):
    parsed = mpesa.parse_stk_callback(stk(code))
    assert parsed["success"] is False
    assert parsed["result_code"] is None


def test_stk_single_item_object_instead_of_list():
    body = stk(0, {"Name": "MpesaReceiptNumber", "Value": "QWE123"})
    assert mpesa.parse_stk_callback(body)["receipt_number"] == "QWE123"


def test_stk_missing_body_does_not_crash():
    assert mpesa.parse_stk_callback({})["success"] is False
    assert mpesa.parse_stk_callback({"Body": None})["result_code"] is None


def payout(result_code, params=None, **top):
    result = {"ResultCode": result_code, "ResultDesc": "x",
              "ConversationID": "AG_1", "OriginatorConversationID": "escrow-payout-1-1"}
    if params is not None:
        result["ResultParameters"] = {"ResultParameter": params}
    result.update(top)
    return {"Result": result}


def test_payout_receipt_inside_parameters():
    parsed = mpesa.parse_payout_result_callback(payout(0, [
        {"Key": "TransactionReceipt", "Value": "ABC111"},
        {"Key": "TransactionAmount", "Value": 500},
    ]))
    assert parsed["outcome"] == "SUCCESS" and parsed["success"] is True
    assert parsed["transaction_id"] == "ABC111"
    assert parsed["completed_amount"] == 500.0


def test_payout_receipt_at_the_top_level_is_found():
    # e.g. Reversal/B2B results that carry the receipt as Result.TransactionID
    parsed = mpesa.parse_payout_result_callback(payout(0, [], TransactionID="ABC222"))
    assert parsed["outcome"] == "SUCCESS"
    assert parsed["transaction_id"] == "ABC222"


def test_payout_string_zero_is_still_success():
    parsed = mpesa.parse_payout_result_callback(payout("0", [{"Key": "TransactionReceipt", "Value": "ABC333"}]))
    assert parsed["outcome"] == "SUCCESS" and parsed["transaction_id"] == "ABC333"


def test_payout_single_parameter_object_instead_of_list():
    body = payout(0)
    body["Result"]["ResultParameters"] = {"ResultParameter": {"Key": "TransactionReceipt", "Value": "ABC444"}}
    assert mpesa.parse_payout_result_callback(body)["transaction_id"] == "ABC444"


@pytest.mark.parametrize("code", [2001, "2001", 1])
def test_payout_failure(code):
    parsed = mpesa.parse_payout_result_callback(payout(code))
    assert parsed["outcome"] == "FAILURE" and parsed["success"] is False
    assert parsed["transaction_id"] is None


@pytest.mark.parametrize("body", [
    payout(None), payout(""), payout("garbage"), {"Result": {}}, {}, {"Result": None},
])
def test_payout_unreadable_result_is_unknown_never_failure(body):
    parsed = mpesa.parse_payout_result_callback(body)
    assert parsed["outcome"] == "UNKNOWN"
    assert parsed["success"] is False
    assert parsed["result_code"] is None


def test_payout_ids_are_passed_through():
    parsed = mpesa.parse_payout_result_callback(payout(2001))
    assert parsed["conversation_id"] == "AG_1"
    assert parsed["originator_conversation_id"] == "escrow-payout-1-1"