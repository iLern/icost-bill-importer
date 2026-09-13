# 财务统计

信用卡账单自动记账工具，写入 iCost。支持两种粒度：

> **平台**：仅 macOS。天粒度自动记账依赖 macOS 的 `open` 命令触发 iCost URL scheme，定时任务依赖 launchd。月度批处理跨平台可用，但需自行配置。

- **月度批处理**（`src/main.py`）：整月账单 `.txt` → LLM 分类 → Excel，手动导入 iCost。
- **天粒度自动记账**（`src/daily.py`）：每天定时读 QQ 邮箱账单邮件 → 解析 → LLM 分类 → 通过 iCost x-callback-url 自动写入。

## 天粒度自动记账

### 流程

```
launchd 每天定时
   └─ daily.py
        ├─ mail_input.fetch_bill_mail    # IMAP 读 QQ 邮箱账单邮件
        ├─ mail_input.parse_mail_body    # 正则解析为交易记录
        ├─ main.parse_category           # 复用 LLM 分类
        ├─ icost_output.to_icost_urls    # 生成 icost:// URL
        └─ icost_output.trigger          # open 触发 iCost
```

### 配置

1. 复制 `.env.example` 为 `.env`，填入：
   - `MAIL_USER` / `MAIL_AUTHCODE`：QQ 邮箱地址与 **IMAP 授权码**（非登录密码。获取：QQ 邮箱网页端 → 设置 → 账户 → 开启 IMAP/SMTP 服务 → 生成授权码）。
   - `OPENAI_API_URL` / `OPENAI_API_KEY`：与 `main.py` 共用的 LLM 配置。

2. 卡号 → iCost 账户名映射在 `src/config.json` 的 `card_account_map`。**账户名必须与 iCost 内已存在的账户名逐字一致**，否则 iCost 会静默忽略请求。iCost 注册的 URL scheme 是小写 `icost`（大小写敏感）。

### 安装

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### 使用

```bash
# dry-run：只打印 iCost URL，不触发
.venv/bin/python src/daily.py --dry-run

# 只处理前 3 条（调试）
.venv/bin/python src/daily.py --dry-run --max 3

# 真实触发，写入 iCost
.venv/bin/python src/daily.py

# 按日期回溯补账：定时任务漏跑（断网 / 机器没开）后用，可一次给多个日期
# 支持 YYYY-MM-DD / YYYY.MM.DD / YYYY/MM/DD / YYYYMMDD
.venv/bin/python src/daily.py --dry-run --date 2026-09-11 2026-09-13
.venv/bin/python src/daily.py --date 2026-09-11
```

`--date` 按邮件正文标题里的交易日期匹配（不是收信时间），逐封解析后合并写入；
已写入过的交易仍会被 `logs/processed.jsonl` 去重跳过，所以可以放心重复执行。
某个日期在邮箱里找不到对应邮件时，会打印实际扫到的日期范围并以非 0 退出，
该日期不写入（其余日期照常处理）。

### 定时任务（launchd）

仓库提供了模板 `launchd/com.example.bill-daily.plist`，使用前需替换其中的 `<REPO_DIR>`（仓库绝对路径）和 `<LABEL>`（你的标识，如 `com.yourname.bill-daily`），并确保 `<REPO_DIR>/logs` 目录已存在。

```bash
# 1. 复制并替换占位符（以 sed 为例，LABEL 自行修改）
cp launchd/com.example.bill-daily.plist ~/Library/LaunchAgents/com.yourname.bill-daily.plist
sed -i '' "s#<REPO_DIR>#$(pwd)#g" ~/Library/LaunchAgents/com.yourname.bill-daily.plist
# 之后用编辑器把 <LABEL> 改成 com.yourname.bill-daily

mkdir -p logs

# 2. 加载
launchctl load ~/Library/LaunchAgents/com.yourname.bill-daily.plist

# 手动触发一次测试
launchctl start com.yourname.bill-daily
# 查看日志
tail -f logs/daily.out.log
```

卸载：

```bash
launchctl unload ~/Library/LaunchAgents/com.yourname.bill-daily.plist
```

## 月度批处理

```bash
.venv/bin/python src/main.py bill/202601.txt output/202601.xlsx
```
