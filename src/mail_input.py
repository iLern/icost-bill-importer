"""账单邮件输入模块。

从 QQ 邮箱（IMAP）拉取信用卡账单邮件，解析其正文为交易记录列表。
解析为确定性正则，无需 LLM。
"""
import email
import imaplib
import re
from email.header import decode_header


IMAP_HOST = "imap.qq.com"
IMAP_PORT = 993

# 日期标题：2026/07/13 您的消费明细如下：
DATE_RE = re.compile(r"(\d{4})/(\d{2})/(\d{2})\s*您的消费明细如下")
TIME_RE = re.compile(r"(\d{2}):(\d{2}):(\d{2})")
AMOUNT_RE = re.compile(r"CNY\s+(-?[\d,]+\.\d+)")
DESC_RE = re.compile(r"尾号\s*(\d+)\s+(消费|退款|退货)\s+(.+)")


def _decode_str(value):
    """解码邮件头/字符串（可能带编码标记）。"""
    if value is None:
        return ""
    parts = decode_header(value)
    out = []
    for text, enc in parts:
        if isinstance(text, bytes):
            out.append(text.decode(enc or "utf-8", errors="replace"))
        else:
            out.append(text)
    return "".join(out)


def _get_body(msg) -> str:
    """从 email.message.Message 提取纯文本正文（优先 text/plain，回退 html 去标签）。"""
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            cdisp = str(part.get("Content-Disposition") or "")
            if ctype == "text/plain" and "attachment" not in cdisp:
                payload = part.get_payload(decode=True)
                if payload:
                    return payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        # 没有 text/plain，回退 html
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                payload = part.get_payload(decode=True)
                if payload:
                    html = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
                    return _strip_html(html)
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            content = payload.decode(charset, errors="replace")
            if msg.get_content_type() == "text/html":
                return _strip_html(content)
            return content
    return ""


def _strip_html(html: str) -> str:
    """粗糙地去掉 HTML 标签，保留分行文本，便于正则。"""
    html = re.sub(r"(?im)<br\s*/?>", "\n", html)
    html = re.sub(r"(?i)</p>", "\n", html)
    html = re.sub(r"<[^>]+>", "", html)
    import html as htmlmod
    return htmlmod.unescape(html)


def fetch_imap_conn(user: str, authcode: str):
    """连接 QQ 邮箱并选中 INBOX，返回已登录的 IMAP4_SSL 对象（调用方负责 logout）。"""
    mbox = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    mbox.login(user, authcode)
    mbox.select("INBOX")
    return mbox


def fetch_bill_mail(user: str, authcode: str, from_addr: str = "ccsvc@message.cmbchina.com",
                    subject_keyword: str = "消费明细", recent: int = 20) -> str:
    """连接 QQ 邮箱 IMAP，取最近一封账单邮件正文。

    先用 IMAP SEARCH FROM 按发件人（ASCII）服务端过滤，避免中文 SEARCH 编码报错；
    再从命中邮件里从新到旧取最近 recent 封，返回首封正文含账单标识的。

    Args:
        user: QQ 邮箱地址，如 xxx@qq.com
        authcode: QQ 邮箱 IMAP 授权码（非登录密码）
        from_addr: 账单邮件发件人地址（招商银行信用卡默认 ccsvc@message.cmbchina.com）
        subject_keyword: 用于二次确认账单邮件的正文关键词
        recent: 从发件人命中邮件里从新到旧最多回溯多少封来查找
    Returns:
        邮件正文纯文本。
    """
    mbox = fetch_imap_conn(user, authcode)
    try:
        # 按发件人服务端过滤（FROM 为 ASCII，不触发中文编码问题）
        typ, data = mbox.search(None, "FROM", f'"{from_addr}"')
        if typ != "OK" or not data or not data[0]:
            raise RuntimeError(f"未找到发件人为 {from_addr} 的邮件")
        ids = data[0].split()
        # 命中邮件从新到旧，最多回溯 recent 封
        scan = list(reversed(ids))[:recent]
        for uid in scan:
            typ, msg_data = mbox.fetch(uid, "(RFC822)")
            if typ != "OK":
                continue
            msg = email.message_from_bytes(msg_data[0][1])
            body = _get_body(msg)
            if subject_keyword in body:
                return body
        raise RuntimeError(f"发件人 {from_addr} 的最近 {len(scan)} 封邮件均不含「{subject_keyword}」")
    finally:
        try:
            mbox.logout()
        except Exception:
            pass


def parse_mail_body(text: str) -> list:
    """解析账单邮件正文为交易记录列表。

    邮件格式：一个日期标题行，随后每条交易占若干行（时间 / 空行 / 金额 / 描述）。
    返回的 record 字段与 main.parse_bills 输出对齐，便于复用 parse_category。
    """
    date_match = DATE_RE.search(text)
    if not date_match:
        raise RuntimeError("未在邮件中找到交易日期标题（您的消费明细如下）")
    y, mo, d = date_match.group(1), date_match.group(2), date_match.group(3)
    trans_date = f"{mo}月{d}日"        # 对齐 main.py 的字段风格
    icost_date = f"{y}.{mo}.{d}"       # iCost date 参数格式

    # 在日期标题之后的部分解析交易
    body = text[date_match.end():]

    # 提取所有时间/金额/描述的出现位置，按行序配对
    times = list(TIME_RE.finditer(body))
    amounts = list(AMOUNT_RE.finditer(body))
    descs = list(DESC_RE.finditer(body))

    n = min(len(times), len(amounts), len(descs))
    records = []
    for i in range(n):
        hh, mm, _ss = times[i].group(1), times[i].group(2), times[i].group(3)
        amount_raw = amounts[i].group(1).replace(",", "")
        amount = float(amount_raw)
        card = descs[i].group(1)
        txn_type = descs[i].group(2)
        desc_text = descs[i].group(3).strip()

        records.append({
            "trans_date": trans_date,
            "post_date": trans_date,
            "description": desc_text,
            "category": "",
            "RMB_amount": amount_raw,
            "card_number": card,
            "country": "CN",
            "amount_value": amount,
            "txn_type": txn_type,        # 消费 / 退款
            "date": icost_date,          # 2026.07.13
            "time": f"{hh}:{mm}",        # 12:01
        })
    return records


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        with open(sys.argv[1], "r", encoding="utf-8") as f:
            sample = f.read()
    else:
        sample = sys.stdin.read()
    for r in parse_mail_body(sample):
        print(r)
