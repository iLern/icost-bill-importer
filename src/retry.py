"""指数退避重试工具。

天粒度流程是 launchd 无人值守运行，网络调用（IMAP 拉邮件 / LLM 分类）偶发
DNS 解析失败、连接中断。这些瞬时故障包一层重试即可吸收，避免整次运行失败。
只有 transient 的网络异常才重试；业务异常（无邮件、分类错误等）直接抛出不重试。
"""
import time


def retry(fn, *, attempts=3, base_delay=2.0, exceptions=(Exception,)):
    """执行 fn()，遇 exceptions 中的异常按指数退避（base_delay * 2^n）重试。

    Args:
        fn: 无参可调用对象，每次尝试都会重新调用。
        attempts: 总尝试次数（含首次），默认 3。
        base_delay: 首次重试前的等待秒数，后续每次翻倍，默认 2。
        exceptions: 视为「可重试」的异常类型元组，默认所有 Exception。

    Returns:
        fn() 的返回值。

    Raises:
        最后一次尝试抛出的异常（若全部失败）。
    """
    last_exc = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except exceptions as exc:
            last_exc = exc
            if attempt < attempts:
                delay = base_delay * (2 ** (attempt - 1))
                print(f"⚠️  网络异常（{type(exc).__name__}），"
                      f"{delay:.0f}s 后第 {attempt}/{attempts - 1} 次重试...")
                time.sleep(delay)
    raise last_exc
