"""iCost 输出模块：把交易记录转成 iCost x-callback-url 并触发。

注意：iCost 注册的 URL scheme 是小写 icost（大小写敏感，iCost:// 不会路由）。
account 必须与 iCost 内存在的账户名逐字一致，否则 iCost 会静默忽略该请求。
icost://expense?[...]  消费
icost://income?[...]   退款/退货（作为收入）
"""
import subprocess
import time
from urllib.parse import quote


# iCost 解析 URL query 时，账户名等参数中的空格必须原样保留
# （编码成 %20 或 + 会导致账户名不匹配而被静默忽略）。
# 但 & # = 等字符会破坏 query 结构，仍需编码；中文保留原样即可被接受。
_UNSAFE = "&#=+%"


def _icost_quote(value: str) -> str:
    """编码会破坏 query 结构的字符（&#=+%），保留空格与中文原样。

    iCost 解析 query 时，账户名里的空格若编码成 %20 或 +，会导致账户名不匹配
    而被静默忽略；故空格保留原样。中文原样传入也能被接受。
    """
    out = []
    for ch in str(value):
        if ch in _UNSAFE:
            out.append(f"%{ord(ch):02X}")
        else:
            out.append(ch)
    return "".join(out)


def to_icost_urls(records: list, card_account_map: dict = None) -> list:
    """把记录列表转为 iCost URL 列表。

    Args:
        records: mail_input.parse_mail_body + main.parse_category 处理后的记录。
        card_account_map: 卡号 -> iCost 账户名映射；缺省用卡号本身当账户名（mock）。
    """
    card_account_map = card_account_map or {}
    urls = []
    for r in records:
        is_income = r.get("txn_type") in ("退款", "退货")
        scheme = "income" if is_income else "expense"
        amount = abs(r["amount_value"])
        # 一级分类：取 LLM 分类中 "/" 前的部分
        category = r.get("category", "")
        category = category.split("/")[0] if category else ""
        account = card_account_map.get(r["card_number"], r["card_number"])

        params = {
            "amount": f"{amount:g}",
            "currency": "CNY",
            "account": account,
            "category": category,
            "date": r["date"],
            "time": r["time"],
            "remark": r["description"],
        }
        # 空格与中文原样保留（见 _icost_quote 说明）
        query = "&".join(f"{k}={_icost_quote(v)}" for k, v in params.items())
        urls.append(f"icost://{scheme}?{query}")
    return urls


def trigger(urls: list, dry_run: bool = False, delay: float = 1.0) -> list:
    """逐条触发 iCost URL，返回每条是否成功的布尔列表（与 urls 等长、顺序一致）。

    dry_run=True 时仅打印 URL（视为成功），便于首次验证参数；否则用 open 触发。
    返回值用于调用方做「成功才记入已处理」的去重判定。
    """
    total = len(urls)
    if total == 0:
        print("⚠️  没有需要写入的交易")
        return []

    results = []
    for idx, url in enumerate(urls, 1):
        if dry_run:
            print(f"[{idx}/{total}] (dry-run) {url}")
            results.append(True)
            continue
        try:
            subprocess.run(["open", url], check=True)
            print(f"[{idx}/{total}] ✅ 已触发：{url}")
            results.append(True)
        except Exception as e:
            print(f"[{idx}/{total}] ❌ 触发失败：{e}\n    {url}")
            results.append(False)
        if idx < total:
            time.sleep(delay)
    return results
