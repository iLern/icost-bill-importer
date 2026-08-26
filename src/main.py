import argparse
import os
from openai import OpenAI, APIConnectionError, APITimeoutError, InternalServerError
import pandas as pd
from tqdm import tqdm
import datetime

from retry import retry

def parse_bills(file_path):
    records = []
    with open(file_path, "r", encoding="utf-8") as f:
        lines = [line.strip() for line in f if line.strip()]  # 去除空行

    # 每7行为一组，显示进度条
    num_chunks = (len(lines) + 6) // 7
    for i in tqdm(range(0, len(lines), 7), total=num_chunks, desc="解析记录", unit="条"):
        chunk = lines[i:i + 7]
        if len(chunk) < 7:
            continue  # 不完整的记录跳过
        record = {
            "trans_date": chunk[0],
            "post_date": chunk[1],
            "description": chunk[2],
            "category": "",
            "RMB_amount": chunk[3],
            "card_number": chunk[4],
            "country": chunk[5],
            "amount_value": float(chunk[6]),
        }
        records.append(record)

    return records

def parse_category(bill_list: list, client: OpenAI, categories: list = None) -> list:
    if categories is None:
        categories = ["餐饮", "购物", "生活", "居住", "交通", "医疗健康",
                      "教育成长", "休闲娱乐", "人情往来", "杂项"]
    model = os.getenv("OPENAI_MODEL")
    if not model:
        print("错误：请在 .env 中配置 OPENAI_MODEL（DeepSeek 官方：deepseek-v4-pro / deepseek-v4-flash）")
        raise SystemExit(1)
    cats_str = "、".join(categories)
    system_content = (
        "你是一个财务助理，负责根据交易描述为每笔交易分类。"
        f"只能从以下分类中选择一个：{cats_str}。"
        "无法明确归类时选「杂项」。只输出分类名称本身，不要输出任何其他文字或标点。"
    )
    ret = []
    for record in tqdm(bill_list, desc="分类交易", unit="笔"):
        desc = record["description"].lower()
        tqdm.write(f"正在处理交易描述：{desc} --> ", end="")
        response = retry(
            lambda: client.chat.completions.create(
                model=model,
                messages=[
                    {
                        "role": "system",
                        "content": system_content,
                    },
                    {
                        "role": "user",
                        "content": f"请根据以下交易描述为其分类：'{desc}'。"
                    }
                ],
                temperature=0.2,
                # 推理型模型（如 deepseek-v4-flash）的 reasoning 会先消耗 token，
                # 10 太小会被吃光导致 content 为空、误归「杂项」，64 足够输出分类名
                max_tokens=64,
            ),
            attempts=3,
            exceptions=(APIConnectionError, APITimeoutError, InternalServerError),
        )
        category = response.choices[0].message.content.strip()
        # 兜底：LLM 偶发输出列表外的分类（幻觉/带标点），归为杂项避免 iCost 静默漏记
        if category not in categories:
            tqdm.write(f"⚠️ 分类「{category}」不在列表中，归为杂项")
            category = "杂项"
        record["category"] = category
        tqdm.write(f"分类结果：{record['category']}")
        ret.append(record)
    return ret

def reformat(record_list: list) -> list:
    reformatted = []
    for record in tqdm(record_list, desc="重格式化", unit="笔"):
        reformatted_record = {
            # 2011年01月11日 12:00:00
            "日期": f"{datetime.datetime.now().year}年{record['trans_date'][0:2]}月{record['trans_date'][-2:]}日 00:00:00",
            "类型": record["amount_value"] > 0 and "支出" or "收入",
            "金额": abs(record["amount_value"]),
            "一级分类": record["category"].split("/")[0] if "/" in record["category"] else record["category"],
            "二级分类": record["category"].split("/")[1] if "/" in record["category"] else "",
            "账户1": record["card_number"],
            "账户2": "",
            "备注": record["description"],
            "标签": "",
        }

        reformatted.append(reformatted_record)

    return reformatted

def save_to_excel(data: list, output_path: str):
    df = pd.DataFrame(data)
    df.to_excel(output_path, index=False)
    
    print(f"✅ 已成功生成：{output_path}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Parse bills file and output JSON")
    parser.add_argument("path", help="输入要解析的账单文件路径（文本文件，每7行为一条记录）")
    parser.add_argument("output", help="输出解析后账单excel")
    args = parser.parse_args()

    if not os.path.exists(args.path):
        print(f"错误：文件不存在：{args.path}")
        raise SystemExit(1)

    data = parse_bills(args.path)
    # print(json.dumps(data, ensure_ascii=False, indent=4))

    client = OpenAI(
        base_url = os.getenv("OPENAI_API_URL"),
        api_key = os.getenv("OPENAI_API_KEY")
    )
    data = parse_category(data, client)
    # print(json.dumps(data, ensure_ascii=False, indent=4)) 
    
    data = reformat(data)
    # print(json.dumps(data, ensure_ascii=False, indent=4))
    
    save_to_excel(data, args.output) 
    