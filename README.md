# 财务统计

信用卡账单自动记账工具，写入 iCost。支持两种粒度：

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
        ├─ icost_output.to_icost_urls    # 生成 iCost:// URL
        └─ icost_output.trigger          # open 触发 iCost
```

### 配置

1. 复制 `.env.example` 为 `.env`，填入：
   - `MAIL_USER` / `MAIL_AUTHCODE`：QQ 邮箱地址与 **IMAP 授权码**（非登录密码。获取：QQ 邮箱网页端 → 设置 → 账户 → 开启 IMAP/SMTP 服务 → 生成授权码）。
   - `OPENAI_API_URL` / `OPENAI_API_KEY`：与 `main.py` 共用的 LLM 配置。

2. 卡号 → iCost 账户名映射在 `src/config.json` 的 `card_account_map`。当前 mock 用卡号本身（如 `9006`）当账户名，确认 iCost 能否据此路由后再调整。

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
```

### 定时任务（launchd）

```bash
mkdir -p ~/Library/LaunchAgents logs
cp launchd/com.leyan.bill-daily.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.leyan.bill-daily.plist

# 手动触发一次测试
launchctl start com.leyan.bill-daily
# 查看日志
tail -f logs/daily.out.log
```

卸载：

```bash
launchctl unload ~/Library/LaunchAgents/com.leyan.bill-daily.plist
```

## 月度批处理

```bash
.venv/bin/python src/main.py bill/202601.txt output/202601.xlsx
```
