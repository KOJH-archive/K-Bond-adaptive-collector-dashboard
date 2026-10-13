"""Independent SQLite archive, batched writes and indexed snapshot searches."""
import csv
import datetime as dt
import hashlib
import json
from pathlib import Path
import sqlite3
from contextlib import contextmanager

from parsing import ROOMS, parse_query

COLS = ('room', 'timestamp', 'message_at', 'sender', 'ticker', 'maturity', 'side', 'content', 'source')


class Store:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY, event_key TEXT NOT NULL UNIQUE,
                    room TEXT NOT NULL, timestamp TEXT NOT NULL, message_at TEXT NOT NULL,
                    sender TEXT NOT NULL, ticker TEXT NOT NULL, maturity TEXT NOT NULL,
                    side TEXT NOT NULL, content TEXT NOT NULL, source TEXT NOT NULL,
                    side_group TEXT NOT NULL, search_text TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS room_idx ON messages(room, id DESC);
                CREATE INDEX IF NOT EXISTS side_idx ON messages(side_group, id DESC);
                CREATE INDEX IF NOT EXISTS date_idx ON messages(timestamp, id DESC);
                CREATE TABLE IF NOT EXISTS imports(path TEXT PRIMARY KEY, signature TEXT NOT NULL);
            ''')
            # Trigram matching handles substrings inside Korean issuer names.
            try:
                db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(search_text, content='messages', content_rowid='id', tokenize='trigram')")
                self.fts = True
            except sqlite3.OperationalError:
                self.fts = False
            if self.fts:
                exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='messages_insert'").fetchone()
                db.executescript('''
                    CREATE TRIGGER IF NOT EXISTS messages_insert AFTER INSERT ON messages BEGIN
                        INSERT INTO search(rowid,search_text) VALUES(new.id,new.search_text);
                    END;
                ''')
                if not exists:
                    db.execute("INSERT INTO search(search) VALUES('rebuild')")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA synchronous=NORMAL')
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def event_key(row):
        identity = [row['room'], row['message_at'], row['sender'], row['content']]
        return hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()

    def insert(self, rows):
        if not rows:
            return 0
        with self.connect() as db:
            inserted = 0
            for row in rows:
                values = [str(row.get(col, '') or '') for col in COLS]
                side_group = row.get('side', '').split('(')[0]
                search_text = ' | '.join(values).lower()
                cursor = db.execute('''INSERT OR IGNORE INTO messages
                    (event_key,room,timestamp,message_at,sender,ticker,maturity,side,content,source,side_group,search_text)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
                    [row.get('event_key') or self.event_key(row), *values, side_group, search_text])
                inserted += cursor.rowcount
            return inserted

    def import_csv(self, directory):
        """Read legacy files without changing them. Retry-safe across restarts."""
        count, errors = 0, []
        for path in sorted(Path(directory).glob('kbond_messages_*.csv')):
            try:
                stat = path.stat()
                signature = f'{stat.st_mtime_ns}:{stat.st_size}'
                with self.connect() as db:
                    saved = db.execute('SELECT signature FROM imports WHERE path=?', (str(path.resolve()),)).fetchone()
                if saved and saved[0] == signature:
                    continue
                room = ROOMS[1] if '막무가내' in path.name else ROOMS[0]
                batch = []
                with path.open(encoding='utf-8-sig', newline='') as handle:
                    reader = csv.DictReader(handle)
                    required = {'수집시간', '송신자', '종목', '만기', '구분', '대화내용'}
                    if not required.issubset(reader.fieldnames or []):
                        raise ValueError('지원하지 않는 CSV 열 형식')
                    for index, old in enumerate(reader):
                        if None in old or any(old.get(k) is None for k in required):
                            raise ValueError(f'{index + 2}행이 미완성입니다. 다음 실행에서 재시도합니다.')
                        clean = lambda value: value[2:-1] if value.startswith('="') and value.endswith('"') else value
                        row = dict(room=room, timestamp=old['수집시간'], message_at=old['수집시간'],
                                   sender=old['송신자'], ticker=clean(old['종목']), maturity=clean(old['만기']),
                                   side=old['구분'], content=old['대화내용'], source='legacy_csv')
                        # Legacy CSV has no original message time. Preserve distinct rows.
                        key = json.dumps([path.name, index, row], ensure_ascii=False, sort_keys=True)
                        row['event_key'] = 'csv:' + hashlib.sha256(key.encode()).hexdigest()
                        batch.append(row)
                        if len(batch) >= 500:
                            count += self.insert(batch)
                            batch = []
                    count += self.insert(batch)
                if path.stat().st_size == stat.st_size and path.stat().st_mtime_ns == stat.st_mtime_ns:
                    with self.connect() as db:
                        db.execute('INSERT OR REPLACE INTO imports VALUES(?,?)', (str(path.resolve()), signature))
            except (OSError, UnicodeError, csv.Error, ValueError) as exc:
                errors.append(f'{path.name}: {exc}')
        return dict(imported=count, errors=errors)

    def stats(self):
        with self.connect() as db:
            row = db.execute('SELECT COUNT(*) total, COALESCE(MAX(id),0) version FROM messages').fetchone()
            return dict(row)

    def search(self, query='', room='', side='', scope='bond', date_from='', date_to='', snapshot=None, page=1, page_size=100):
        parsed = parse_query(query)
        room, side = room or parsed['room'], side or parsed['side']
        date_from, date_to = date_from or parsed['date_from'], date_to or parsed['date_to']
        if room and room not in ROOMS:
            raise ValueError('알 수 없는 채팅방입니다.')
        if side not in ('', '매수', '매도', '거래', '중단') or scope not in ('bond', 'all', 'sender'):
            raise ValueError('잘못된 검색 조건입니다.')
        for value in (date_from, date_to):
            if value:
                dt.date.fromisoformat(value)
        if date_from and date_to and date_from > date_to:
            raise ValueError('시작일은 종료일보다 늦을 수 없습니다.')
        page, page_size = max(1, int(page)), min(100, max(1, int(page_size)))
        with self.connect() as db:
            # Count, snapshot and rows observe one SQLite read transaction.
            db.execute('BEGIN')
            maximum = db.execute('SELECT COALESCE(MAX(id),0) FROM messages').fetchone()[0]
            snapshot = maximum if snapshot is None else min(maximum, max(0, int(snapshot)))
            clauses, params = ['m.id<=?'], [snapshot]
            for column, value in [('room', room), ('side_group', side)]:
                if value:
                    clauses.append(f'm.{column}=?')
                    params.append(value)
            if date_from:
                clauses.append('m.timestamp>=?')
                params.append(date_from)
            if date_to:
                clauses.append('m.timestamp<?')
                params.append((dt.date.fromisoformat(date_to) + dt.timedelta(days=1)).isoformat())
            field = {'bond': "lower(m.ticker || ' | ' || m.content)", 'sender': 'lower(m.sender)', 'all': 'm.search_text'}[scope]
            long_terms = [t for t in parsed['terms'] if len(t) >= 3]
            if self.fts and long_terms:
                match = ' AND '.join('"' + t.replace('"', '""') + '"' for t in long_terms)
                clauses.append('m.id IN (SELECT rowid FROM search WHERE search MATCH ?)')
                params.append(match)
            for term in parsed['terms']:
                clauses.append(f'instr({field},?)>0')
                params.append(term)
            where = ' AND '.join(clauses)
            total = db.execute(f'SELECT COUNT(*) FROM messages m WHERE {where}', params).fetchone()[0]
            # Collecting order is stable even if the messenger's reported time goes backwards.
            rows = db.execute(f'SELECT m.id,{",".join("m."+c for c in COLS)} FROM messages m WHERE {where} ORDER BY m.id DESC LIMIT ? OFFSET ?',
                              [*params, page_size, (page-1)*page_size]).fetchall()
        return dict(rows=[dict(r) for r in rows], total=total, snapshot=snapshot,
                    page=page, page_size=page_size, pages=(total+page_size-1)//page_size,
                    filters={**parsed, 'room': room, 'side': side, 'scope': scope, 'date_from': date_from, 'date_to': date_to},
                    as_of=dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
