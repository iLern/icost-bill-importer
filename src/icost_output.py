"""iCost 输出模块：把交易记录转成 iCost x-callback-url 并触发。

iCost://expense?[...]  消费
iCost://income?[...]   退款（作为收入）
"""
import subprocess
import time
from urllib.parse import quote, urlencode


def to_icost_urls(records: list, card_account_map: dict = None) -> list:
    """把记录列表转为 iCost URL 列表。

    Args:
        records: mail_input.parse_mail_body + main.parse_category 处理后的记录。
        card_account_map: 卡号 -> iCost 账户名映射；缺省用卡号本身当账户名（mock）。
    """
    card_account_map = card_account_map or {}
    urls = []
    for r in records:
        is_income = r.get("txn_type") == "退款"
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
        # iCost 期望中文以原始字符或编码均可，这里统一编码
        query = urlencode(params, quote_via=quote)
        urls.append(f"iCost://{scheme}?{query}")
    return urls


def trigger(urls: list, dry_run: bool = False, delay: float = 1.0) -> None:
    """逐条触发 iCost URL。

    dry_run=True 时仅打印 URL，便于首次验证参数；否则用 open 触发。
    """
    total = len(urls)
    if total == 0:
        print("⚠️  没有需要写入的交易")
        return

    for idx, url in enumerate(urls, 1):
        if dry_run:
            print(f"[{idx}/{total}] (dry-run) {url}")
            continue
        try:
            subprocess.run(["open", url], check=True)
            print(f"[{idx}/{total}] ✅ 已触发：{url}")
        except Exception as e:
            print(f"[{idx}/{total}] ❌ 触发失败：{e}\n    {url}")
        if idx < total:
            time.sleep(delay)
