from app.numbers import normalize_numbers, parse_sino


def test_parse_sino():
    assert parse_sino("칠천오백") == 7500
    assert parse_sino("백팔십") == 180
    assert parse_sino("천백") == 1100
    assert parse_sino("이십사") == 24
    assert parse_sino("삼만오천") == 35000
    assert parse_sino("십") == 10
    assert parse_sino("이삼") is None


def test_normalize():
    assert normalize_numbers("칠천오백 개?") == "7500개?"
    assert normalize_numbers("벌써 백팔십 개 했던데") == "벌써 180개 했던데"
    assert normalize_numbers("팔월 이십사일이 마감이야") == "8월 24일이 마감이야"
    assert normalize_numbers("시월 삼일") == "10월 삼일"  # 십 없는 한 글자 날짜는 그대로 (삼일절 등과 헷갈림)
    assert normalize_numbers("오십 퍼센트") == "50퍼센트"
    assert normalize_numbers("십오분마다") == "15분마다"


def test_normalize_leaves_words_alone():
    for s in ["다음 주로 이월돼요", "이월 결제", "팔월이는", "이번에는", "일단은", "만들어 줘", "천천히 해", "백엔드 개발", "오분 뒤에"]:
        assert normalize_numbers(s) == s, s
