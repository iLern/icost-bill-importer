import argparse
import os
from openai import OpenAI
import pandas as pd
from tqdm import tqdm

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

def parse_category(bill_list: list, client: OpenAI) -> list:
    ret = []
    for record in tqdm(bill_list, desc="分类交易", unit="笔"):
        desc = record["description"].lower()
        tqdm.write(f"正在处理交易描述：{desc} --> ", end="")
        response = client.chat.completions.create(
            model="Qwen/Qwen3-VL-235B-A22B-Instruct",
            messages=[
                {
                    "role": "system",
                    "content": "你是一个财务助理，负责根据交易描述为每笔交易分类。类别包括：餐饮、交通、购物、居住、生活、医疗健康、教育成长、人际娱乐、投资理财。只需要给出最后的分类结果。"
                },
                {
                    "role": "user",
                    "content": f"请根据以下交易描述为其分类：'{desc}'。"
                }
            ],
            temperature=0.2,
            max_tokens=10,
        )
        record["category"] = response.choices[0].message.content.strip()
        tqdm.write(f"分类结果：{record['category']}")
        ret.append(record)
    return ret

def reformat(record_list: list) -> list:
    reformatted = []
    for record in tqdm(record_list, desc="重格式化", unit="笔"):
        reformatted_record = {
            # 2011年01月11日 12:00:00
            "日期": f"2025年{record['trans_date'][0:2]}月{record['trans_date'][-2:]}日 00:00:00",
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
        base_url = "https://api.siliconflow.cn/v1",
        api_key = os.getenv("OPENAI_API_KEY")
    )
    data = parse_category(data, client)
    # print(json.dumps(data, ensure_ascii=False, indent=4)) 
    
    data = reformat(data)
    # print(json.dumps(data, ensure_ascii=False, indent=4))
    
    save_to_excel(data, args.output) 
    