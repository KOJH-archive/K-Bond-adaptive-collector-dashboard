"""Local deterministic parsing; no remote service or language model required."""
import datetime as dt
import re

ROOMS = ("[채권] 블커본드", "[채권] 막무가내")
HEADER = re.compile(r"^\s*(.*?)\s*\(\s*(\d{1,2}:\d{1,2}:\d{1,2})\s*\)\s*:\s*(.*)$")
TICKER = re.compile(r"\d{2,4}(?:\.\d{1,2}){1,2}통|[가-힣A-Za-z]+[\w-]*(?:\([가-힣A-Za-z\d-]+\))?|\d{2,3}-\d{1,2}")
NOISE = set("팔자 사자 매도 매수 비드 오퍼 offer bid 수반 민평 민 쩜 끝 원 목 금 월 화 수 토 일 휴일 평일 당일 문의 부탁 확인 또는 유사 만기 있는 상기 협의 가능 교체 배정 내역 기타 구조 매체 거래 체결 완료 확정 추팔 done 후 보류 취소 캔슬 cancel".split())


def fields(content):
    side = ""
    for pattern, label in [(r"팔자|매도|오퍼|offer", "매도"), (r"사자|매수|비드|bid", "매수"), (r"확정|done|체결|거래", "거래")]:
        if re.search(pattern, content, re.I):
            side = label
            break
    # A hyphen in a bond series such as 26-2 is not a sell instruction.
    if not side:
        if re.search(r"(?<![\w.])\+(?=\d|\s|$)", content):
            side = "매수"
        elif re.search(r"(?<![\w.])-(?=\d|\s|$)", content):
            side = "매도"
    if re.search(r"보류|취소|캔슬|cancel", content, re.I):
        side = (side + "(중단)") if side else "중단"
    ticker = ""
    for match in TICKER.finditer(content):
        word = match.group()
        if len(word) < 2 or word.lower() in NOISE:
            continue
        if any(x in word for x in ("있을까요", "부탁", "문의", "교체가능")):
            continue
        if word.startswith(("민", "쩜", "끝")) and len(word) <= 4:
            continue
        if re.fullmatch(r"\d{2,3}-\d{1,2}", word) and content[match.end():match.end()+1] == "-":
            continue
        ticker = word
        break
    maturity = re.search(r"\d{2,4}[./]\d{1,2}(?:[./]\d{1,2})?", content)
    return ticker, (maturity.group() if maturity else ""), side


def parse_buffer(text, room, now=None):
    """Keep multiline bodies; infer the nearest past day from ordered HH:MM:SS.

    The messenger does not expose a date in this text format. Reading old,
    manually scrolled buffers cannot establish their original calendar date.
    """
    now = now or dt.datetime.now()
    blocks = []
    for line in text.replace('\u200b', '').splitlines():
        match = HEADER.match(line)
        if match:
            sender, clock, content = match.groups()
            try:
                clock = dt.time(*map(int, clock.split(':')))
            except ValueError:
                continue
            blocks.append([sender.strip(), clock, content.strip()])
        elif blocks and line.strip():
            blocks[-1][2] += '\n' + line.strip()
    cursor = now + dt.timedelta(minutes=5)
    rows = []
    for sender, clock, content in reversed(blocks):
        message_at = dt.datetime.combine(cursor.date(), clock)
        if message_at - cursor > dt.timedelta(hours=12):
            message_at -= dt.timedelta(days=1)
        # Small out-of-order timestamps do not mean that a whole day elapsed.
        cursor = min(cursor, message_at)
        ticker, maturity, side = fields(content)
        # Preserve original text even when its ticker cannot be inferred.
        rows.append(dict(room=room, timestamp=now.strftime('%Y-%m-%d %H:%M:%S'),
                         message_at=message_at.strftime('%Y-%m-%d %H:%M:%S'),
                         sender=sender, ticker=ticker, maturity=maturity, side=side,
                         content=content, source='live'))
    return list(reversed(rows))


def parse_query(query, today=None):
    today = today or dt.date.today()
    query = query.strip().lower()
    if len(query) > 300:
        raise ValueError('검색어는 300자 이내로 입력해 주세요.')
    result = dict(terms=[], room='', side='', date_from='', date_to='')
    for room in ROOMS:
        short = room.replace('[채권] ', '')
        if short in query:
            result['room'] = room
            query = query.replace(short, ' ')
    for word in ('매수', '매도', '거래', '중단'):
        if re.search(r'(?<!\w)' + word + r'(?:만|내역)?(?!\w)', query):
            result['side'] = word
            query = re.sub(r'(?<!\w)' + word + r'(?:만|내역)?(?!\w)', ' ', query)
            break
    for word, offset in [('오늘', 0), ('어제', 1)]:
        if word in query:
            day = (today - dt.timedelta(days=offset)).isoformat()
            result.update(date_from=day, date_to=day)
            query = query.replace(word, ' ')
            break
    stop = set('채권 채권을 채권에 정보 정보를 내역 내역을 검색 조회 찾아줘 찾아 보여줘 보여 알려줘 알려 관련 대한 들어간 있는 것 거 좀 해줘 방 전체 전부 모두 이름 이름이 대한 의'.split())
    result['terms'] = [t for t in re.findall(r'[\w.+/-]+', query) if t not in stop]
    if len(result['terms']) > 12:
        raise ValueError('검색 조건은 12개 이내로 입력해 주세요.')
    return result
