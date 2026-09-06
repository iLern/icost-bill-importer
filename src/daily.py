"""天粒度账单处理编排入口。

流程：IMAP 读账单邮件 → 解析邮件正文 → LLM 分类 → 生成 iCost URL → 触发 iCost。
被 launchd 每天定时无人值守运行。也可手动 `python src/daily.py --dry-run` 调试。
"""
import argparse
import json
import os
import sys

# 确保能 import 同目录下的 main / mail_input / icost_output
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv  # noqa: E402
from openai import OpenAI  # noqa: E402

from main import parse_category  # noqa: E402
from mail_input import fetch_bill_mail, parse_mail_body  # noqa: E402
from icost_output import to_icost_urls, trigger  # noqa: E402
from dedup import ProcessedStore  # noqa: E402


def load_config() -> dict:
    """读取 src/config.json，缺省返回空映射。"""
    cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    if os.path.exists(cfg_path):
        with open(cfg_path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"card_account_map": {}}


def main():
    parser = argparse.ArgumentParser(description="天粒度账单：读邮件 → 分类 → 写入 iCost")
    parser.add_argument("--dry-run", action="store_true", help="只打印 iCost URL，不触发")
    parser.add_argument("--max", type=int, default=0, help="只处理前 N 条（调试用，0=全部）")
    args = parser.parse_args()

    load_dotenv()  # 从项目根 .env 读取密钥

    mail_user = os.getenv("MAIL_USER")
    mail_authcode = os.getenv("MAIL_AUTHCODE")
    if not mail_user or not mail_authcode:
        print("错误：请在 .env 中配置 MAIL_USER 和 MAIL_AUTHCODE（QQ 邮箱 IMAP 授权码）")
        raise SystemExit(1)

    # 1. 读邮件并解析
    print("📧 正在拉取账单邮件...")
    body = fetch_bill_mail(mail_user, mail_authcode)
    records = parse_mail_body(body)
    print(f"📩 解析到 {len(records)} 笔交易")

    if not records:
        print("今天没有交易，结束。")
        return

    # 去重：跳过已成功写入 iCost 的交易，避免 launchd 多次触发导致重复记账
    # （fetch_bill_mail 总是返回最新那封邮件的全部交易；同一封邮件被重跑时，
    # 已写入的应当跳过而非再次 open 触发。）
    store = ProcessedStore()
    fresh, skipped = [], []
    for r in records:
        if store.has(r):
            skipped.append(r)
        else:
            fresh.append(r)
    if skipped:
        print(f"⏭️  跳过 {len(skipped)} 笔已写入交易："
              + ", ".join(f"{s['description']}({s['amount_value']})" for s in skipped))
    records = fresh
    if not records:
        print("所有交易均已写入，结束。")
        return

    # 账户准入：卡号未配置在 card_account_map 的记录，URL 会退回用卡号当账户名，
    # iCost 对账户名不匹配的请求是静默忽略（等于没写）；若照旧触发并按 open 成功
    # 标记“已写入”，这笔会被永久误判为完成。故此类记录不触发、不标记、打错提醒，
    # 在 config.json 补上映射后下次运行自动补写。
    config = load_config()
    card_account_map = config.get("card_account_map", {}) or {}
    unmapped = [r for r in records if r["card_number"] not in card_account_map]
    for r in unmapped:
        print(f"❌ 卡号 {r['card_number']} 未配置在 card_account_map，本次不写入："
              f"{r['description']} ({r['amount_value']}) —— 补配置后下次自动补写")
    records = [r for r in records if r["card_number"] in card_account_map]
    if not records:
        print("没有账户已配置的交易，结束。")
        return

    if args.max > 0:
        records = records[: args.max]

    # 3. 分类：先按 config 的 category_overrules 关键词规则强制归类，未命中的才走 LLM
    categories = config.get("categories")
    overrides = config.get("category_overrides", {}) or {}

    def apply_overrides(recs):
        pending, overridden = [], []
        for r in recs:
            hit = next((cat for kw, cat in overrides.items() if kw in r["description"]), None)
            if hit is not None:
                r["category"] = hit
                overridden.append(r)
                print(f"🔁 规则命中：{r['description']} --> {hit}")
            else:
                pending.append(r)
        return pending, overridden

    pending, overridden = apply_overrides(records)

    base_url = os.getenv("OPENAI_API_URL")
    api_key = os.getenv("OPENAI_API_KEY")
    classified = []
    if pending:
        if not base_url or not api_key:
            print("错误：请在 .env 中配置 OPENAI_API_URL 和 OPENAI_API_KEY")
            raise SystemExit(1)
        client = OpenAI(base_url=base_url, api_key=api_key)
        classified = parse_category(pending, client, categories=categories)
    records = classified + overridden

    # 4. 生成 iCost URL（records 已保证卡号都在 card_account_map 内）
    urls = to_icost_urls(records, card_account_map)

    # 5. 触发（成功才记入已处理，失败下次重试）
    print(f"\n🚀 准备写入 iCost（dry_run={args.dry_run}）")
    results = trigger(urls, dry_run=args.dry_run)
    if not args.dry_run:
        for r, ok in zip(records, results):
            if ok:
                store.mark(r)
    print("\n🎉 完成")


if __name__ == "__main__":
    main()
