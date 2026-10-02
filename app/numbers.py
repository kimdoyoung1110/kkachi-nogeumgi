"""한글로 적힌 숫자를 아라비아 숫자로 바꾼다 (예: 칠천오백 개 → 7500개, 팔월 이십사일 → 8월 24일).

받아쓰기 모델이 숫자를 '칠천오백', '백팔십'처럼 적는 경우가 있어 요약·검색이 어려워진다.
잘못 바꾸면 뜻이 바뀌므로 보수적으로만 바꾼다:
- 십·백·천·만·억 중 하나가 들어간 숫자만 ('이월'(넘김), '이번', '일단' 같은 말은 건드리지 않음)
- 바로 뒤에 개수·단위가 올 때만
"""

from __future__ import annotations

import re

_DIGITS = {"영": 0, "공": 0, "일": 1, "이": 2, "삼": 3, "사": 4, "오": 5, "육": 6, "칠": 7, "팔": 8, "구": 9}
_SMALL = {"십": 10, "백": 100, "천": 1000}
_BIG = {"만": 10_000, "억": 100_000_000}

UNITS = (
    "개|원|명|번|회차|회|퍼센트|프로|시간|분|초|년|월|일|주|살|배|등|층|권|장|대|건|가지|달|개월|"
    "킬로|그램|미터|센티|페이지|쪽|줄|칸|점|위|기|차|호|명분|인분"
)
_NUM = r"[영공일이삼사오육칠팔구십백천만억]+"
_PATTERN = re.compile(rf"(?<![가-힣])({_NUM})(\s?)(?=(?:{UNITS}))")


def parse_sino(word: str) -> int | None:
    """'칠천오백' → 7500, '백팔십' → 180, '이십사' → 24. 숫자가 아니면 None."""
    total, section, digit = 0, 0, None
    for ch in word:
        if ch in _DIGITS:
            if digit is not None:  # '이삼' 처럼 자릿수 없이 숫자가 이어지면 숫자 표현이 아님
                return None
            digit = _DIGITS[ch]
        elif ch in _SMALL:
            section += (digit if digit is not None else 1) * _SMALL[ch]
            digit = None
        elif ch in _BIG:
            section += digit or 0
            total += (section or 1) * _BIG[ch]
            section, digit = 0, None
        else:
            return None
    return total + section + (digit or 0)


# 달 이름: 뜻이 헷갈리지 않는 것만 ('이월'은 '다음 주로 이월'처럼 쓰이고, '일월'도 드물게 겹쳐서 제외)
_MONTHS = {"삼월": 3, "사월": 4, "오월": 5, "유월": 6, "칠월": 7, "팔월": 8, "구월": 9, "시월": 10,
           "십일월": 11, "십이월": 12}
_MONTH_PATTERN = re.compile(r"(?<![가-힣])(" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + r")(?![가-힣])")


def normalize_numbers(text: str) -> str:
    def repl(m: re.Match) -> str:
        word = m.group(1)
        if not any(c in word for c in "십백천만억"):
            return m.group(0)
        value = parse_sino(word)
        return m.group(0) if value is None else str(value)

    text = _MONTH_PATTERN.sub(lambda m: f"{_MONTHS[m.group(1)]}월", text)
    return _PATTERN.sub(repl, text)
