from __future__ import annotations


def direct_answer_from_tool(tool_result: dict | None) -> str:
    if not tool_result:
        return ""
    tool_id = str(tool_result.get("tool") or "")
    result = tool_result.get("result") if isinstance(tool_result.get("result"), dict) else {}
    if result and result.get("ok") is False:
        if tool_id == "time":
            return "我这边暂时读不到本机时间。"
        if tool_id == "weather":
            return f"天气查询失败：{result.get('error') or '数据不可用'}"
        return ""
    if tool_id == "time":
        return f"现在是 {result.get('text') or ''}，{result.get('weekday') or ''}。".strip()
    if tool_id == "weather":
        current = result.get("current") or {}
        weather = current.get("weather") or "天气数据不完整"
        temp = current.get("temp_c")
        feels = current.get("feels_like_c")
        humidity = current.get("humidity")
        area = result.get("area") or result.get("location") or "当地"
        parts = [f"{area}现在{weather}"]
        if temp not in (None, ""):
            parts.append(f"{temp}°C")
        if feels not in (None, ""):
            parts.append(f"体感{feels}°C")
        if humidity not in (None, ""):
            parts.append(f"湿度{humidity}%")
        return "，".join(parts) + "。"
    return ""
