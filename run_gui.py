"""GUI 실행 진입점 (exe 빌드 타겟)"""
from src.utils.config_loader import load_config
from src.utils.logger import setup_logging
from src.db.database import init_db
from gui.main_window import MainWindow


def main():
    cfg = load_config()
    setup_logging(**cfg["logging"])
    init_db(cfg["database"]["path"])
    MainWindow().run()


if __name__ == "__main__":
    main()
