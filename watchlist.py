"""Local watchlist persistence, independent from brokerage positions and trades."""

import json
import os
import re
from threading import RLock
from typing import List


class Watchlist:
    """Persist codes selected for observation without representing an account."""

    def __init__(self, data_dir: str = None):
        root = data_dir or os.path.dirname(os.path.abspath(__file__))
        self.cache_dir = os.path.join(root, ".cache")
        self.path = os.path.join(self.cache_dir, "watchlist.json")
        self.legacy_path = os.path.join(root, "portfolio_watchlist.json")
        self._lock = RLock()
        self.codes: List[str] = []
        self._load()

    def _load(self) -> None:
        source = self.path if os.path.exists(self.path) else self.legacy_path
        if not os.path.exists(source):
            return
        try:
            with open(source, encoding="utf-8") as source_file:
                data = json.load(source_file)
            self.codes = [
                str(code).strip()
                for code in data
                if re.fullmatch(r"\d{6}", str(code).strip())
            ]
        except (OSError, json.JSONDecodeError, TypeError):
            self.codes = []

    def _save(self) -> None:
        os.makedirs(self.cache_dir, exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as output_file:
            json.dump(self.codes, output_file, ensure_ascii=False, indent=2)

    def add(self, code: str) -> None:
        code = str(code).strip()
        if not re.fullmatch(r"\d{6}", code):
            raise ValueError("自选股代码必须为6位数字")
        with self._lock:
            if code not in self.codes:
                self.codes.append(code)
                self._save()

    def remove(self, code: str) -> None:
        with self._lock:
            if code in self.codes:
                self.codes.remove(code)
                self._save()


watchlist = Watchlist()
