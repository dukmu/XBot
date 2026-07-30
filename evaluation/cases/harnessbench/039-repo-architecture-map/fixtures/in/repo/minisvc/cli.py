import os

from minisvc.config import load_settings
from minisvc.storage.repo import OrderRepository


def main(argv=None) -> int:
    settings = load_settings(os.environ)
    repo = OrderRepository(settings.database_path)
    repo.init_schema()
    print(f"minisvc ready at {settings.database_path}")
    return 0
