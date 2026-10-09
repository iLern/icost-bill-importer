"""账单邮件输入模块。

从 QQ 邮箱（IMAP）拉取信用卡账单邮件，解析其正文为交易记录列表。
解析为确定性正则，无需 LLM。
"""
import email
import imaplib
import re
from email.header import decode_header

from retry import retry


IMAP_HOST = "imap.qq.com"
IMAP_PORT = 993

# 按日期回溯补账时放宽的扫描窗口（账单邮件约每天一封，180 封 ≈ 半年）
_BACKFILL_SCAN = 180

# 日期标题：2026/07/13 您的消费明细如下：
DATE_RE = re.compile(r"(\d{4})/(\d{2})/(\d{2})\s*您的消费明细如下")
TIME_RE = re.compile(r"(\d{2}):(\d{2}):(\d{2})")
# 金额行带币种：CNY 5.27 / EUR 321.00。外币只列原币金额，人民币折算值见「可用额度」，
# 由 convert_foreign 用相邻两封邮件的额度差反推（不能只认 CNY，否则外币交易被静默丢弃）。
AMOUNT_RE = re.compile(r"\b([A-Z]{3})\s+(-?[\d,]+\.\d+)")
# 描述行：尾号2300 消费 财付通-拼多多平台商户。
# 类型不写死枚举：除 消费/退款/退货 外还有 邮购 等，写死会让这类交易整笔消失。
DESC_RE = re.compile(r"尾号\s*(\d+)\s+(\S+)\s+(.+)")
# 可用额度：正文里「可用额度」后的第一个 ￥ 金额，如 ￥89,488.54
CREDIT_RE = re.compile(r"可用额度[^￥]*￥([\d,]+\.\d+)", re.S)


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


def _body_date(text: str) -> str:
    """取邮件正文标题行的交易日期，返回 iCost 格式（2026.09.11）；无标题返回空串。"""
    m = DATE_RE.search(text)
    if not m:
        return ""
    return f"{m.group(1)}.{m.group(2)}.{m.group(3)}"


def fetch_imap_conn(user: str, authcode: str):
    """连接 QQ 邮箱并选中 INBOX，返回已登录的 IMAP4_SSL 对象（调用方负责 logout）。"""
    mbox = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    mbox.login(user, authcode)
    mbox.select("INBOX")
    return mbox


def _iter_bill_mails(mbox, from_addr: str, subject_keyword: str, window: int):
    """从新到旧遍历账单邮件，yield (账单日期, 正文, 已扫到的日期列表)。

    已扫到的日期列表随迭代增长，用于「没找到」时报出实际覆盖范围。
    """
    typ, data = mbox.search(None, "FROM", f'"{from_addr}"')
    if typ != "OK" or not data or not data[0]:
        raise RuntimeError(f"未找到发件人为 {from_addr} 的邮件")
    ids = data[0].split()
    seen = []
    for uid in list(reversed(ids))[:window]:
        typ, msg_data = mbox.fetch(uid, "(RFC822)")
        if typ != "OK":
            continue
        msg = email.message_from_bytes(msg_data[0][1])
        body = _get_body(msg)
        if subject_keyword not in body:
            continue
        bill_date = _body_date(body)
        if not bill_date:
            continue
        seen.append(bill_date)
        yield bill_date, body, seen


def fetch_prev_credit(user: str, authcode: str, before_date: str,
                      from_addr: str = "ccsvc@message.cmbchina.com",
                      subject_keyword: str = "消费明细") -> tuple:
    """取 before_date 之前最近一封账单邮件的 (日期, 可用额度)，供外币折算反推汇率。

    刻意取「紧邻的前一封」而不是任意更早的一封：中间若有还款/额度调整，
    额度会整体跳变，用更早的额度会把这笔跳变算进当天消费里。
    外币折算依赖此值，取不到直接抛错 —— 宁可让当天不写，也别写个错汇率进去。
    """
    def _fetch_once() -> tuple:
        mbox = fetch_imap_conn(user, authcode)
        try:
            for bill_date, body, _seen in _iter_bill_mails(
                    mbox, from_addr, subject_keyword, _BACKFILL_SCAN):
                if bill_date < before_date:
                    credit = parse_credit(body)
                    if credit is None:
                        raise RuntimeError(
                            f"{bill_date} 的账单邮件里没有「可用额度」，无法折算外币金额")
                    return bill_date, credit
            raise RuntimeError(f"{before_date} 之前没有账单邮件，无法折算外币金额")
        finally:
            try:
                mbox.logout()
            except Exception:
                pass
    return retry(_fetch_once, attempts=3, exceptions=(OSError, imaplib.IMAP4.error))


def fetch_bill_mail(user: str, authcode: str, from_addr: str = "ccsvc@message.cmbchina.com",
                    subject_keyword: str = "消费明细", recent: int = 20,
                    target_date: str = None) -> str:
    """连接 QQ 邮箱 IMAP，取账单邮件正文。

    先用 IMAP SEARCH FROM 按发件人（ASCII）服务端过滤，避免中文 SEARCH 编码报错；
    再从命中邮件里从新到旧回溯，返回首封正文含账单标识的。

    Args:
        user: QQ 邮箱地址，如 xxx@qq.com
        authcode: QQ 邮箱 IMAP 授权码（非登录密码）
        from_addr: 账单邮件发件人地址（招商银行信用卡默认 ccsvc@message.cmbchina.com）
        subject_keyword: 用于二次确认账单邮件的正文关键词
        recent: 从发件人命中邮件里从新到旧最多回溯多少封来查找
        target_date: 指定交易日期（iCost 格式 YYYY.MM.DD，如 2026.09.11），
            只返回正文标题日期与之相符的那封；缺省返回最新一封。
            用于定时任务漏跑后的回溯补账 —— 缺省路径只认最新一封，
            漏跑那天的邮件一旦被更新的邮件盖住就再也取不到。
            指定日期时回溯窗口放宽到 _BACKFILL_SCAN 封。
    Returns:
        邮件正文纯文本。

    网络层瞬时异常（DNS / 连接 / IMAP 协议）会指数退避重试 3 次；
    业务异常（未找到邮件）直接抛出不重试。
    """
    def _fetch_once() -> str:
        mbox = fetch_imap_conn(user, authcode)
        try:
            # 命中邮件从新到旧，最多回溯 recent 封（按日期回溯时放宽）
            window = max(recent, _BACKFILL_SCAN) if target_date else recent
            scanned = []
            for bill_date, body, scanned in _iter_bill_mails(mbox, from_addr, subject_keyword, window):
                if target_date is None:
                    return body
                if bill_date == target_date:
                    return body
                if bill_date < target_date:
                    break  # 从新到旧扫描，已翻过目标日期，再往前只会更旧
            if target_date is not None:
                # 报出实际扫到的日期范围，能直接回答「那天到底还有没有邮件」
                covered = (f"已扫最近 {len(scanned)} 封账单邮件，覆盖 {scanned[-1]} ~ {scanned[0]}"
                           if scanned else "发件人最近的邮件里没有账单邮件")
                raise RuntimeError(f"未找到 {target_date} 的账单邮件（{covered}）")
            raise RuntimeError(f"发件人 {from_addr} 的最近 {window} 封以内均不含「{subject_keyword}」")
        finally:
            try:
                mbox.logout()
            except Exception:
                pass
    return retry(_fetch_once, attempts=3, exceptions=(OSError, imaplib.IMAP4.error))


def parse_mail_body(text: str) -> list:
    """解析账单邮件正文为交易记录列表。

    邮件格式：一个日期标题行，随后每条交易占三行（时间 / 金额 / 描述）。
    返回的 record 字段与 main.parse_bills 输出对齐，便于复用 parse_category。

    配对方式是按行序把「时间 → 金额 → 描述」顺序装配成一条，**不能**改成
    「三个正则各扫一遍再按下标 zip」：外币金额一旦不在金额列表里（早期 AMOUNT_RE
    只认 CNY），三个列表长度不等，zip 会把金额与商户整体错位配错
    （2026.09.26 曾把 KLM 机票和力挚退货记串）。
    """
    date_match = DATE_RE.search(text)
    if not date_match:
        raise RuntimeError("未在邮件中找到交易日期标题（您的消费明细如下）")
    y, mo, d = date_match.group(1), date_match.group(2), date_match.group(3)
    trans_date = f"{mo}月{d}日"        # 对齐 main.py 的字段风格
    icost_date = f"{y}.{mo}.{d}"       # iCost date 参数格式

    # 在日期标题之后的部分解析交易
    body = text[date_match.end():]

    # 三类行按出现位置合并成一条事件流，再顺序装配
    events = []
    for kind, regex in (("time", TIME_RE), ("amount", AMOUNT_RE), ("desc", DESC_RE)):
        for m in regex.finditer(body):
            events.append((m.start(), kind, m))
    events.sort(key=lambda e: e[0])

    records, cur, incomplete = [], None, 0
    for _pos, kind, m in events:
        if kind == "time":
            cur = {"time": f"{m.group(1)}:{m.group(2)}"}
            records.append(cur)
            continue
        if cur is None:
            continue  # 日期标题前（如「可用额度 ￥xx」）的金额/描述不参与配对
        if kind == "amount" and "currency" not in cur:
            cur["currency"] = m.group(1)
            cur["amount_orig"] = float(m.group(2).replace(",", ""))
            cur["RMB_amount"] = m.group(2).replace(",", "")
        elif kind == "desc" and "description" not in cur:
            cur["card_number"] = m.group(1)
            cur["txn_type"] = m.group(2)
            cur["description"] = m.group(3).strip()

    out = []
    for r in records:
        if not all(k in r for k in ("currency", "description", "card_number")):
            incomplete += 1
            continue
        out.append({
            "trans_date": trans_date,
            "post_date": trans_date,
            "description": r["description"],
            "category": "",
            "RMB_amount": r["RMB_amount"],   # 原币金额字符串（未折算）
            "card_number": r["card_number"],
            "country": "CN",
            "amount_value": r["amount_orig"],  # 记账金额：外币在 convert_foreign 里覆盖成人民币
            "amount_orig": r["amount_orig"],   # 原币金额，折算后保留供备注使用
            "currency": r["currency"],
            "txn_type": r["txn_type"],
            "date": icost_date,              # 2026.07.13
            "time": r["time"],               # 12:01
        })
    # 缺金额或缺描述的半条交易宁可报出来也别静默丢：正则是按格式硬匹配的，
    # 招行改版后第一现场就是这里
    if incomplete:
        print(f"⚠️ 有 {incomplete} 条交易缺金额或描述行，已跳过（邮件格式可能变了）")
    return out


def parse_credit(body: str):
    """取邮件正文「可用额度」的金额（元）。取不到返回 None。"""
    m = CREDIT_RE.search(body)
    return float(m.group(1).replace(",", "")) if m else None


def convert_foreign(records: list, credit_prev: float, credit_cur: float):
    """把外币交易按当日额度变动折成人民币，返回汇率（无需折算时返回 None）。

    账单邮件外币只给原币金额，人民币折算额由「可用额度」的日间差值反推：
        当日入账人民币总额 = 上一封额度 - 本封额度
        汇率 = (当日入账人民币总额 - 当日人民币交易净额) / 当日外币交易净额
    各笔按原币金额乘同一汇率，当日合计与银行扣减的额度一致。

    credit_prev 必须是**紧邻的前一封**账单邮件：这中间若发生还款/额度调整，
    用更早的额度会把这笔跳变算进当天消费，汇率会离谱。
    """
    if credit_prev is None or credit_cur is None:
        return None
    foreign = [r for r in records if r["currency"] != "CNY"]
    if not foreign:
        return None
    fx_total = sum(r["amount_orig"] for r in foreign)
    if not fx_total:
        return None
    cny_net = sum(r["amount_value"] for r in records if r["currency"] == "CNY")
    rate = (credit_prev - credit_cur - cny_net) / fx_total
    for r in foreign:
        r["amount_value"] = round(r["amount_orig"] * rate, 2)
    return rate


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        with open(sys.argv[1], "r", encoding="utf-8") as f:
            sample = f.read()
    else:
        sample = sys.stdin.read()
    for r in parse_mail_body(sample):
        print(r)
