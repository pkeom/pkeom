"""SQLAlchemy ORM 모델 정의"""
from datetime import datetime
from sqlalchemy import Column, Integer, String, DateTime, Float, Boolean, Text
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class Order(Base):
    """스마트스토어 수집 주문"""
    __tablename__ = "orders"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ss_order_id = Column(String, unique=True, nullable=False)   # 스마트스토어 주문번호
    ss_product_id = Column(String, nullable=False)
    ss_option_id = Column(String, default="")
    quantity = Column(Integer, default=1)
    buyer_name = Column(String)
    receiver_name = Column(String)
    receiver_phone = Column(String)
    receiver_address = Column(String)
    receiver_zipcode = Column(String)
    delivery_memo = Column(String, default="")
    order_status = Column(String, default="COLLECTED")          # COLLECTED / ORDERED / INVOICED / DONE / ERROR
    collected_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class SupplierOrder(Base):
    """도매처 발주 내역"""
    __tablename__ = "supplier_orders"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ss_order_id = Column(String, nullable=False)                # 연결된 스마트스토어 주문번호
    supplier = Column(String, nullable=False)                   # domaekkuk / domaemae
    supplier_product_id = Column(String, nullable=False)
    supplier_order_no = Column(String, default="")
    quantity = Column(Integer, default=1)
    status = Column(String, default="PENDING")                  # PENDING / ORDERED / SHIPPED / ERROR
    delivery_company = Column(String, default="")
    tracking_number = Column(String, default="")
    ordered_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ProductMapping(Base):
    """스마트스토어 ↔ 도매처 상품 매핑"""
    __tablename__ = "product_mappings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ss_product_id = Column(String, nullable=False)
    ss_option_id = Column(String, default="")
    supplier = Column(String, nullable=False)
    supplier_product_id = Column(String, nullable=False)
    supplier_option_id = Column(String, default="")
    price_margin_rate = Column(Float, default=1.3)
    is_active = Column(Boolean, default=True)
    memo = Column(Text, default="")
    created_at = Column(DateTime, default=datetime.utcnow)


class PriceHistory(Base):
    """도매처 가격 변동 이력"""
    __tablename__ = "price_history"

    id = Column(Integer, primary_key=True, autoincrement=True)
    supplier = Column(String, nullable=False)
    supplier_product_id = Column(String, nullable=False)
    old_price = Column(Integer, nullable=True)
    new_price = Column(Integer, nullable=True)
    detected_at = Column(DateTime, default=datetime.utcnow)


class ErrorLog(Base):
    """오류 로그"""
    __tablename__ = "error_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    module = Column(String)
    message = Column(Text)
    notified = Column(Boolean, default=False)
    occurred_at = Column(DateTime, default=datetime.utcnow)
