"""프로젝트 공통 예외"""


class EmoneyShortageError(RuntimeError):
    """도매처 e머니(도머니) 잔액 부족으로 발주 실패."""
