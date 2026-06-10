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
        if tool_id == "map":
            return f"地图查询失败：{result.get('error') or '数据不可用'}"
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
    if tool_id == "map":
        kind = result.get("kind") or ""
        if kind == "geocode":
            items = result.get("results") or []
            if not items:
                return "没有查到明确的地点结果。"
            item = items[0]
            query = result.get("query") or "这个地方"
            province = item.get("province") or ""
            city = item.get("city") or ""
            district = item.get("district") or ""
            location = item.get("location") or ""
            area = "".join(part for part in [province, city, district] if isinstance(part, str) and part)
            suffix = f"，坐标 {location}" if location else ""
            return f"{query}属于{area}{suffix}。" if area else f"查到的位置是：{item.get('formatted_address') or query}{suffix}。"
        if kind == "place_search":
            items = result.get("results") or []
            if not items:
                return "没有查到明确的地点结果。"
            lines = []
            for item in items[:3]:
                name = item.get("name") or item.get("title") or item.get("formatted_address") or "地点"
                address = item.get("address") or item.get("formatted_address") or item.get("area") or ""
                location = item.get("location") or ""
                extra = f"，{address}" if address else ""
                coord = f"，坐标 {location}" if location else ""
                lines.append(f"{name}{extra}{coord}")
            return "查到这些结果：" + "；".join(lines) + "。"
        if kind == "regeo":
            address = result.get("formatted_address") or ""
            return f"这个坐标对应的位置是：{address}。" if address else ""
        if kind == "route":
            distance = result.get("distance_m") or ""
            duration = result.get("duration_s") or ""
            parts = [f"{result.get('origin') or '起点'}到{result.get('destination') or '终点'}"]
            if distance:
                parts.append(f"距离约{round(float(distance) / 1000, 1)}公里")
            if duration:
                parts.append(f"预计{round(float(duration) / 60)}分钟")
            steps = result.get("steps") or []
            if steps:
                parts.append("路线：" + "，".join(steps[:3]))
            return "，".join(parts) + "。"
    return ""
