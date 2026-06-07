"""data/mappings.json 기반 상품 매핑 저장소

파일 구조:
{
  "mappings": [
    {
      "id": 1,
      "ss_product_id": "12345678",
      "ss_option_id": "",
      "supplier": "domaekkuk",          // domaekkuk | domaemae
      "supplier_product_id": "56328525",
      "supplier_url": "https://domeggook.com/56328525",
      "supplier_option_id": "",
      "price_margin_rate": 1.3,
      "is_active": true,
      "memo": "",
      "created_at": "2026-05-07T17:00:00"
    }
  ]
}
"""
import json
import re
import threading
from datetime import datetime
from pathlib import Path

_DEFAULT_PATH = Path("data/mappings.json")
_lock = threading.Lock()


def extract_supplier_id(url_or_id: str) -> str:
    """도매꾹/도매매 URL 또는 숫자ID에서 상품번호 추출.

    지원 형식:
      https://domeggook.com/56328525              → '56328525'
      https://domeme.domeggook.com/s/63641452     → '63641452'
      https://www.domaemae.co.kr/...?no=56328525  → '56328525'
      56328525                                    → '56328525'
    """
    text = url_or_id.strip()
    # 도매꾹: domeggook.com/{no} (숫자 직접)
    m = re.search(r"domeggook\.com/(\d+)", text)
    if m:
        return m.group(1)
    # 도매매: domeggook.com/s/{no}
    m = re.search(r"domeggook\.com/s/(\d+)", text)
    if m:
        return m.group(1)
    # 쿼리 파라미터: ?no={no}
    m = re.search(r"[?&]no=(\d+)", text)
    if m:
        return m.group(1)
    # 숫자만
    if re.fullmatch(r"\d+", text):
        return text
    return text


class MappingRepository:
    def __init__(self, path: str | Path = _DEFAULT_PATH):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)

    # ── 내부 I/O ────────────────────────────────────────────

    def _read(self) -> list[dict]:
        if not self._path.exists():
            return []
        with open(self._path, encoding="utf-8") as f:
            return json.load(f).get("mappings", [])

    def _write(self, mappings: list[dict]):
        with open(self._path, "w", encoding="utf-8") as f:
            json.dump({"mappings": mappings}, f, ensure_ascii=False, indent=2)

    # ── 공개 API ─────────────────────────────────────────────

    def all(self) -> list[dict]:
        """전체 매핑 목록 반환"""
        with _lock:
            return list(self._read())

    def find(self, ss_product_id: str, ss_option_id: str = "") -> dict | None:
        """ss_product_id + ss_option_id로 활성 매핑 1건 반환 (없으면 None)"""
        with _lock:
            for m in self._read():
                if (m["ss_product_id"] == ss_product_id
                        and m.get("ss_option_id", "") == ss_option_id
                        and m.get("is_active", True)):
                    return m
        return None

    def add(
        self,
        ss_product_id: str,
        supplier: str,
        supplier_url_or_id: str,
        ss_option_id: str = "",
        supplier_option_id: str = "",
        price_margin_rate: float = 1.3,
        memo: str = "",
        origin_product_no: str = "",
    ) -> dict:
        """매핑 추가 후 저장. 생성된 항목 반환."""
        supplier_product_id = extract_supplier_id(supplier_url_or_id)
        if supplier_product_id.isdigit():
            if supplier == "domaemae":
                supplier_url = f"https://domeme.domeggook.com/s/{supplier_product_id}"
            else:
                supplier_url = f"https://domeggook.com/{supplier_product_id}"
        else:
            supplier_url = supplier_url_or_id.strip()

        with _lock:
            mappings = self._read()
            new_id = max((m.get("id", 0) for m in mappings), default=0) + 1
            entry = {
                "id": new_id,
                "ss_product_id": ss_product_id,
                "ss_option_id": ss_option_id,
                "origin_product_no": origin_product_no,
                "supplier": supplier,
                "supplier_product_id": supplier_product_id,
                "supplier_url": supplier_url,
                "supplier_option_id": supplier_option_id,
                "price_margin_rate": price_margin_rate,
                "is_active": True,
                "memo": memo,
                "created_at": datetime.now().isoformat(timespec="seconds"),
            }
            mappings.append(entry)
            self._write(mappings)
            return entry

    def remove(self, mapping_id: int) -> bool:
        """ID로 매핑 삭제. 삭제 성공 여부 반환."""
        with _lock:
            mappings = self._read()
            filtered = [m for m in mappings if m.get("id") != mapping_id]
            if len(filtered) == len(mappings):
                return False
            self._write(filtered)
            return True

    def set_active(self, mapping_id: int, is_active: bool):
        """활성 상태 토글"""
        with _lock:
            mappings = self._read()
            for m in mappings:
                if m.get("id") == mapping_id:
                    m["is_active"] = is_active
                    break
            self._write(mappings)

    def update_memo(self, mapping_id: int, memo: str):
        """메모 수정"""
        with _lock:
            mappings = self._read()
            for m in mappings:
                if m.get("id") == mapping_id:
                    m["memo"] = memo
                    break
            self._write(mappings)

    def set_supplier_option_id(self, mapping_id: int, supplier_option_id: str):
        """supplier_option_id 업데이트 (backfill용)"""
        with _lock:
            mappings = self._read()
            for m in mappings:
                if m.get("id") == mapping_id:
                    m["supplier_option_id"] = supplier_option_id
                    break
            self._write(mappings)
