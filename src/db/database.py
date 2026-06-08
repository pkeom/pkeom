"""DB 연결 및 세션 관리"""
from contextlib import contextmanager
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from src.db.models import Base


_engine = None
_SessionFactory = None


def init_db(db_path: str):
    global _engine, _SessionFactory
    _engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(_engine)
    _SessionFactory = sessionmaker(bind=_engine)


def is_db_initialized() -> bool:
    return _SessionFactory is not None


@contextmanager
def get_session():
    if _SessionFactory is None:
        raise RuntimeError("DB 미초기화 — init_db()를 먼저 호출하세요")
    session = _SessionFactory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
