import re

from babel import Locale
from babel.core import get_global

COUNTRIES_ZH = Locale("zh", script="Hans").territories
COUNTRIES_EN = {
    name.casefold(): code for code, name in Locale("en").territories.items() if len(code) == 2
}
STATUS_LABELS = {
    "active": "活跃",
    "system_error": "系统错误",
    "ending": "即将到期",
    "expired": "已到期",
}


def natural_key(name: str):
    return tuple(int(part) if index % 2 else part
                 for index, part in enumerate(re.split(r"([0-9]+)", name.casefold())))


def country_label(value: str | None) -> str:
    if not value:
        return ""
    name = value.strip()
    code = COUNTRIES_EN.get(name.casefold(), name.upper())
    if code not in COUNTRIES_ZH:
        aliases = get_global("territory_aliases").get(code, ())
        if len(aliases) == 1:
            code = aliases[0]
    return COUNTRIES_ZH.get(code, name)


def status_label(value: str | None) -> str:
    if not value:
        return ""
    key = "_".join(value.casefold().replace("_", " ").replace("-", " ").split())
    return STATUS_LABELS.get(key, value)
