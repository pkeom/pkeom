"""스마트스토어 반품 신청 감지 → data/returns.json 저장 + 이메일 알림

흐름:
  스마트스토어 API에서 RETURN_REQUEST 상태 주문 조회
    이미 처리한 ID → 스킵
    신규 반품     → returns.json 저장 + 이메일 알림

수집 필드 (order_collector와 동일 기준 + 반품 전용):
  상품: product_order_id, ss_order_id, product_id, product_name,
        option_code, option_name, quantity, unit_price, total_payment_amount
  배송: buyer_name, buyer_phone, receiver_name, receiver_phone,
        receiver_address, receiver_road_address, receiver_detail_address,
        receiver_zipcode, delivery_memo
  기존발송: invoice_number, delivery_company
  반품: return_reason, return_reason_type, claim_status, claim_request_date,
        return_delivery_company, return_tracking_number
  환불: refund_amount, refund_expected_date, return_delivery_fee_payer
"""
import json
import logging
import os
from datetime import datetime, timezone

from src.api.smartstore import SmartstoreAPI

logger = logging.getLogger(__name__)

RETURNS_FILE = "data/returns.json"


def _safe_int(value, default: int = 0) -> int:
    """None·빈 문자열·비정수 값을 안전하게 int로 변환."""
    try:
        return int(value) if value is not None and value != "" else default
    except (TypeError, ValueError):
        return default


def _extract_refund_amount(claim: dict) -> int:
    """claim 노드에서 환불 예정 금액을 추출.

    Naver Commerce API 응답 구조:
      claim.claimPrice.refundPayAmount          (최우선)
      claim.claimPrice.requestRefundPayAmount
      claim.refundPayAmount                     (플랫 구조 폴백)
      claim.refundExpectedPaymentAmount
      claim.refundAmount
    """
    cp = claim.get("claimPrice") or {}
    return _safe_int(
        cp.get("refundPayAmount")
        or cp.get("requestRefundPayAmount")
        or claim.get("refundPayAmount")
        or claim.get("refundExpectedPaymentAmount")
        or claim.get("refundAmount")
    )


def _parse_return_item(raw: dict, detected_at: str) -> dict | None:
    """product-orders/query 응답 1건을 반품 엔트리로 정규화.

    API 중첩 구조:
      raw["productOrder"]     → 상품주문 정보
      raw["order"]            → 구매자 정보
      raw["shippingAddress"]  → 수령인/배송지
      raw["deliveryMemo"]     → 배송 메모
      raw["claim"]            → 반품/클레임 정보
    """
    po    = raw.get("productOrder", raw)
    ord_  = raw.get("order", {})
    addr  = raw.get("shippingAddress", {})
    claim = raw.get("claim", {})

    order_id = (
        po.get("productOrderId")
        or po.get("orderId")
        or raw.get("productOrderId")
        or raw.get("orderId")
    )
    if not order_id:
        return None

    return {
        # ── 주문 식별 ────────────────────────────────────────────────
        "product_order_id":   str(order_id),
        "ss_order_id":        str(po.get("orderId") or ord_.get("orderId") or order_id),

        # ── 상품 정보 ────────────────────────────────────────────────
        "product_id":         str(po.get("productId", "")),
        "product_name":       po.get("productName", "알 수 없음"),
        "option_code":        str(po.get("optionCode") or po.get("optionId") or ""),
        "option_name":        str(po.get("optionName", "")),
        "quantity":           _safe_int(po.get("quantity"), 1),
        "unit_price":         _safe_int(po.get("unitPrice")),
        "total_payment_amount": _safe_int(po.get("totalPaymentAmount")),

        # ── 구매자 / 수령인 정보 ─────────────────────────────────────
        "buyer_name":         ord_.get("ordererName", ""),
        "buyer_phone":        (ord_.get("ordererTel")
                               or ord_.get("ordererTelephone", "")),
        "receiver_name":      addr.get("name", ""),
        "receiver_phone":     (addr.get("tel1") or addr.get("tel2") or ""),
        "receiver_address":   addr.get("addressStr", ""),
        "receiver_road_address":   addr.get("roadAddress", ""),
        "receiver_detail_address": addr.get("detailAddress", ""),
        "receiver_zipcode":   addr.get("zipCode", ""),
        "delivery_memo":      raw.get("deliveryMemo", ""),

        # ── 기존 발송 정보 ───────────────────────────────────────────
        "invoice_number":     str(po.get("invoiceNumber") or ""),
        "delivery_company":   str(po.get("deliveryCompany") or ""),

        # ── 반품 사유 / 클레임 상태 ──────────────────────────────────
        "return_reason":      (claim.get("returnReason")
                               or claim.get("claimReason")
                               or "사유 미상"),
        "return_reason_type": claim.get("returnReasonType", ""),
        "claim_status":       claim.get("claimStatus", ""),
        "claim_request_date": claim.get("claimRequestDate", ""),

        # ── 반품 배송 정보 ───────────────────────────────────────────
        "return_delivery_company": claim.get("returnDeliveryCompany", ""),
        "return_tracking_number":  claim.get("returnTrackingNumber", ""),

        # ── 환불 정보 ────────────────────────────────────────────────
        "refund_amount":           _extract_refund_amount(claim),
        "refund_expected_date":    claim.get("refundExpectedDate", ""),
        "return_delivery_fee_payer": claim.get("deliveryFeePayType", ""),

        # ── 메타 ─────────────────────────────────────────────────────
        "detected_at": detected_at,
    }


class ReturnMonitor:
    def __init__(self, smartstore: SmartstoreAPI, notifier=None,
                 returns_file: str | None = None):
        self._ss = smartstore
        self._notifier = notifier
        self._returns_file = returns_file or RETURNS_FILE

    def run(self) -> dict:
        """반품 신청 감지 및 알림. 반환값: {"new": n, "total": n}"""
        logger.info("반품 감지 시작")
        try:
            raw = self._ss.get_returns(hours=24)
        except Exception as e:
            logger.error("반품 목록 조회 실패: %s", e)
            return {"new": 0, "total": 0}

        data = self._load()
        seen_ids = {r["product_order_id"] for r in data["returns"]}
        new_count = 0
        detected_at = datetime.now(timezone.utc).isoformat()

        for item in raw:
            po = item.get("productOrder", item)
            order_id = (
                po.get("productOrderId")
                or po.get("orderId")
                or item.get("productOrderId", "")
            )
            if not order_id or order_id in seen_ids:
                continue

            entry = _parse_return_item(item, detected_at)
            if not entry:
                logger.warning("반품 항목 파싱 실패 (order_id 없음): %s", list(item.keys()))
                continue

            data["returns"].append(entry)
            seen_ids.add(order_id)
            new_count += 1

            self._notify(entry)
            logger.info(
                "신규 반품 감지: %s / %s / %d개 / %s",
                order_id, entry["product_name"],
                entry["quantity"], entry["return_reason"],
            )

        if new_count:
            self._save(data)

        logger.info(
            "반품 감지 완료: 신규 %d건 / 누적 %d건",
            new_count, len(data["returns"]),
        )
        return {"new": new_count, "total": len(data["returns"])}

    # ── 파일 I/O ──────────────────────────────────────────────────────

    def _load(self) -> dict:
        if os.path.exists(self._returns_file):
            try:
                with open(self._returns_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {"returns": []}

    def _save(self, data: dict):
        os.makedirs(os.path.dirname(self._returns_file) or ".", exist_ok=True)
        with open(self._returns_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    # ── 이메일 알림 ───────────────────────────────────────────────────

    def _notify(self, entry: dict):
        if not self._notifier:
            return
        subject = f"[스마트스토어] 반품 신청 — {entry['product_name']}"
        opt = entry["option_name"] or entry["option_code"] or "없음"
        body = "\n".join([
            "■ 새 반품 신청이 접수되었습니다.",
            "",
            "■ 상품 정보",
            f"  상품명      : {entry['product_name']}",
            f"  상품ID      : {entry['product_id']}",
            f"  옵션        : {opt}",
            f"  수량        : {entry['quantity']}개",
            f"  단가        : {entry['unit_price']:,}원",
            f"  결제금액    : {entry['total_payment_amount']:,}원",
            "",
            "■ 주문 정보",
            f"  상품주문번호: {entry['product_order_id']}",
            f"  주문번호    : {entry['ss_order_id']}",
            f"  구매자      : {entry['buyer_name']}  {entry['buyer_phone']}",
            f"  수령인      : {entry['receiver_name']}  {entry['receiver_phone']}",
            f"  배송주소    : {entry['receiver_address']}",
            f"  도로명주소  : {entry['receiver_road_address']}",
            f"  상세주소    : {entry['receiver_detail_address']}",
            f"  우편번호    : {entry['receiver_zipcode']}",
            f"  배송메모    : {entry['delivery_memo']}",
            "",
            "■ 기존 발송 정보",
            f"  택배사      : {entry['delivery_company']}",
            f"  송장번호    : {entry['invoice_number']}",
            "",
            "■ 반품 정보",
            f"  반품 사유   : {entry['return_reason']}",
            f"  사유 유형   : {entry['return_reason_type']}",
            f"  클레임 상태 : {entry['claim_status']}",
            f"  반품 신청일 : {entry['claim_request_date']}",
            f"  반품 택배사 : {entry['return_delivery_company']}",
            f"  반품 송장   : {entry['return_tracking_number']}",
            "",
            "■ 환불 정보",
            f"  환불 금액   : {entry['refund_amount']:,}원",
            f"  환불 예정일 : {entry['refund_expected_date']}",
            f"  배송비 부담 : {entry['return_delivery_fee_payer']}",
            "",
            f"감지 시각     : {entry['detected_at']}",
        ])
        try:
            self._notifier.send(subject=subject, body=body)
        except Exception as e:
            logger.error("반품 이메일 전송 실패: %s", e)
