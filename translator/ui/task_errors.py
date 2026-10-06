"""Actionable explanations shared by task recovery and queued jobs."""
from requests import ConnectionError, Timeout


def task_error(exc):
    chain, current = [], exc
    while current is not None and current not in chain:
        chain.append(current)
        current = current.__cause__ or current.__context__
    detail = str(exc)
    lowered = detail.lower()
    if any(isinstance(error, (ConnectionError, Timeout)) for error in chain):
        code, advice = "model_offline", "模型服务未连接，请启动 Ollama 并检查服务地址。"
    elif any(word in lowered for word in ("登录", "登入", "/auth/login", "unauthorized", "401")):
        code, advice = "login_required", "登录尚未完成或已失效，重试后请在弹出的浏览器中完成登录。"
    elif isinstance(exc, FileNotFoundError) or "本地 EPUB" in detail or "原文" in detail and "缺失" in detail:
        code, advice = "source_missing", "原文文件不存在或路径无效，请恢复原文件，或重新选择原文后开始翻译。"
    elif any(word in lowered for word in ("not found", "does not exist", "未安装", "不存在的模型", "找不到模型")):
        code, advice = "model_missing", "指定模型尚未安装，请安装该模型，或选择已安装模型重新开始。"
    else:
        code, advice = "task_failed", "请根据详细原因处理后重试。"
    return {"code": code, "advice": advice, "detail": detail}
