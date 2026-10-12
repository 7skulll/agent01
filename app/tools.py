import math
from datetime import datetime
from zoneinfo import ZoneInfo

from langchain.tools import tool


@tool
def current_time() -> str:
    """获取当前北京时间（Asia/Shanghai），用于回答当前日期与时间问题。"""
    return datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")


@tool
def multiply_numbers(a: float, b: float) -> str:
    """计算两个有限数值的乘积，用于简单乘法问题。"""
    value = a * b
    if not all(math.isfinite(x) for x in (a, b, value)):
        return "输入或结果超出有限数值范围。"
    return str(value)


CHAT_TOOLS = [current_time, multiply_numbers]
