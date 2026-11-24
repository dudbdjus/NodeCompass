"""Offline display names for DB-IP entries without a Chinese label."""
import re
import unicodedata


_HAN = re.compile(r"[\u3400-\u9fff\uf900-\ufaff\U00020000-\U000323af]")

_NAMES = {
    "ashburn": "阿什本",
    "virginia": "弗吉尼亚州",
    "amsterdam": "阿姆斯特丹",
    "arizona": "亚利桑那州",
    "california": "加利福尼亚州",
    "district of columbia": "哥伦比亚特区",
    "fairfield": "费尔菲尔德",
    "greensboro": "格林斯伯勒",
    "kansas": "堪萨斯州",
    "los angeles": "洛杉矶",
    "massachusetts": "马萨诸塞州",
    "mesa": "梅萨",
    "mountain view": "山景城",
    "new south wales": "新南威尔士州",
    "new york": "纽约",
    "new york city": "纽约市",
    "north carolina": "北卡罗来纳州",
    "north holland": "北荷兰省",
    "overland park": "欧弗兰帕克",
    "sao paulo": "圣保罗",
    "seattle": "西雅图",
    "springfield": "斯普林菲尔德",
    "sydney": "悉尼",
    "washington": "华盛顿州",
    "washington d.c.": "华盛顿",
    "washington dc": "华盛顿",
    "quebec": "魁北克省",
    "montreal": "蒙特利尔",
    "san francisco": "旧金山",
}

_AREAS = {
    "college oaks": "学院橡树区",
    "downtown": "市中心",
    "kleinman park neighborhood": "克莱曼公园社区",
    "queen anne": "安妮女王区",
    "south beach": "南海滩",
}


def has_han(text):
    return bool(_HAN.search(text))


def _key(text):
    text = unicodedata.normalize("NFKD", text.casefold())
    return " ".join("".join(c for c in text if not unicodedata.combining(c)).split())


def localized_name(section, missing_translation):
    """Prefer DB-IP's Chinese; don't pass untranslated names through to the UI."""
    names = section.get("names") if isinstance(section, dict) else None
    if not isinstance(names, dict):
        return ""
    values = {key.casefold().replace("_", "-"): value.strip() for key, value in names.items()
              if isinstance(key, str) and isinstance(value, str) and 0 < len(value.strip()) <= 256
              and value.strip().isprintable()}
    for key in ("zh-cn", "zh-hans-cn", "zh-hans", "zh", "zh-tw", "zh-hk", "zh-hant",
                *(key for key in values if key.startswith("zh-"))):
        value = values.get(key, "")
        if has_han(value):
            return value
    for value in (values.get("en", ""), *values.values()):
        key = _key(value)
        if key in _NAMES:
            return _NAMES[key]
        parenthetical = re.fullmatch(r"(.+?)\s+\(([^()]+)\)", key)
        if parenthetical and parenthetical[1] in _NAMES:
            base = _NAMES[parenthetical[1]]
            area = _AREAS.get(parenthetical[2])
            return f"{base}（{area}）" if area else base
    return missing_translation if values else ""
