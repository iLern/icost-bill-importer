"""已写入交易去重，避免重复触发把同一批交易多次写入 iCost。

根因：fetch_bill_mail 总是返回最新那封账单邮件的全部交易。当 launchd 多次触发
（手动 launchctl start、邮件延迟后补跑、进程重启重跑）时，同一封邮件会被重复
处理并 open 触发，导致 iCost 重复记账。

做法：每条交易成功写入 iCost 后，按 (date|time|amount|description) 落一条记录到
本地已处理文件。下次处理前先查：命中则跳过，不重新触发。

记录文件默认在项目根 logs/processed.jsonl（与 launchd 日志同目录，已被 gitignore）。
"""
import json
import os


def _dedup_key(rec: dict) -> str:
    """与 macOS app RunLog.dedupKey 对齐：date|time|amount(分)|description。"""
    cents = round(float(rec["amount_value"]) * 100)
    return f"{rec['date']}|{rec['time']}|{cents}|{rec['description']}"


class ProcessedStore:
    """已写入交易记录的追加式存储。轻量：每行一条 JSON，启动读全量到内存 set。"""

    def __init__(self, path: str = None):
        if path is None:
            # 默认放在项目根 logs/processed.jsonl（launchd 日志同目录，已 gitignore）
            root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            path = os.path.join(root, "logs", "processed.jsonl")
        self.path = path
        self._cache = None  # 延迟加载

    def _load(self) -> set:
        """读全量 keys 到 set。文件不存在视为空。"""
        if self._cache is not None:
            return self._cache
        keys = set()
        if os.path.exists(self.path):
            with open(self.path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        keys.add(json.loads(line)["key"])
                    except (json.JSONDecodeError, KeyError):
                        continue
        self._cache = keys
        return keys

    def has(self, rec: dict) -> bool:
        """该交易是否已写入过。"""
        return _dedup_key(rec) in self._load()

    def mark(self, rec: dict) -> None:
        """标记该交易已写入（追加一条，并更新内存缓存）。"""
        key = _dedup_key(rec)
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"key": key}, ensure_ascii=False) + "\n")
        self._load().add(key)
