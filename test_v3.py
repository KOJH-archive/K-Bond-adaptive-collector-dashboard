import csv
import datetime as dt
import tempfile
from pathlib import Path
import unittest

from app import create_app
from collector import Collector
from parsing import ROOMS, parse_buffer
from storage import Store


NOW = dt.datetime(2026, 10, 13, 10, 0, 0)


def message(room=ROOMS[0], content='하나은행26-1 매수 27.09', sender='테스터', clock='09:59:50'):
    return parse_buffer(f'{sender} ({clock}) : {content}', room, NOW)[0]


class FakeReader:
    def __init__(self, connected=ROOMS):
        self.connected = set(connected)
        self.handles = {room: i+1 for i, room in enumerate(ROOMS)}
        self.buffers = {i+1: '테스터 (09:59:50) : 하나은행26-1 매수 27.09' for i in range(2)}
        self.errors = set()
        self.searches = {room: 0 for room in ROOMS}

    def valid(self, handle):
        return any(self.handles[r] == handle for r in self.connected)

    def belongs(self, root, child):
        return root == child

    def find_window(self, room):
        self.searches[room] += 1
        return self.handles[room] if room in self.connected else None

    def find_text_handle(self, root):
        return root

    def text(self, handle):
        if handle in self.errors:
            raise TimeoutError('simulated unresponsive room')
        return self.buffers[handle]


class V3Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.store = Store(self.directory/'test.sqlite3')

    def tearDown(self):
        self.temp.cleanup()

    def engine(self, reader):
        self.clock = 0
        return Collector(self.store, reader, clock=lambda: self.clock, wall=lambda: NOW)

    def advance(self, engine, seconds):
        for _ in range(int(seconds*2)):
            self.clock += .5
            engine.tick()

    def test_either_single_room_and_neither_room(self):
        for available in [(), (ROOMS[0],), (ROOMS[1],)]:
            with self.subTest(available=available):
                reader = FakeReader(available)
                engine = self.engine(reader)
                engine.tick()
                self.advance(engine, 30)
                for state in engine.states:
                    self.assertEqual(state.status, 'connected' if state.room in available else 'offline')
                    if state.room not in available:
                        self.assertLessEqual(reader.searches[state.room], 4)
                if available:
                    self.assertEqual(self.store.search(room=available[0])['total'], 1)

    def test_exception_in_first_room_does_not_skip_second(self):
        reader = FakeReader()
        reader.errors.add(1)
        engine = self.engine(reader)
        engine.tick()
        self.assertEqual(engine.states[0].status, 'retrying')
        self.assertEqual(self.store.search(room=ROOMS[1])['total'], 1)
        reader.errors.clear()
        self.advance(engine, 10)
        self.assertEqual(self.store.search(room=ROOMS[0])['total'], 1)

    def test_disconnect_reconnect_and_restart_dedup(self):
        reader = FakeReader()
        engine = self.engine(reader)
        engine.tick()
        reader.connected.remove(ROOMS[0])
        self.advance(engine, 1)
        self.assertEqual(engine.states[0].status, 'offline')
        reader.connected.add(ROOMS[0])
        reader.buffers[1] += '\n테스터 (10:00:00) : 하나은행26-2 매도'
        self.advance(engine, 11)
        self.assertEqual(self.store.search(room=ROOMS[0])['total'], 2)
        restarted = self.engine(reader)
        restarted.tick()
        self.assertEqual(self.store.stats()['total'], 3)

    def test_idle_reads_are_reduced_and_changes_wake_fast(self):
        reader = FakeReader()
        engine = self.engine(reader)
        engine.tick()
        self.advance(engine, 60)
        self.assertLess(engine.states[0].reads, 30)  # fixed 0.5s would read 121 times
        self.assertEqual(engine.states[0].parses, 1)
        self.assertEqual(engine.states[0].interval, 5)
        reader.buffers[1] += '\n테스터 (10:00:00) : 하나은행26-2 매도'
        self.advance(engine, 5)
        self.assertEqual(self.store.search(room=ROOMS[0])['total'], 2)
        self.assertEqual(engine.states[0].interval, .5)

    def test_failed_database_write_is_retried(self):
        reader = FakeReader((ROOMS[0],))
        engine = self.engine(reader)
        original = self.store.insert
        self.store.insert = lambda rows: (_ for _ in ()).throw(OSError('disk failure'))
        engine.tick()
        self.assertIsNone(engine.states[0].previous)
        self.store.insert = original
        self.advance(engine, 10)
        self.assertEqual(self.store.stats()['total'], 1)

    def test_appended_message_only_parses_the_tail(self):
        reader = FakeReader((ROOMS[0],))
        reader.buffers[1] = '\n'.join(f'테스터 (09:59:50) : 하나은행{i} 매수' for i in range(150))
        engine = self.engine(reader)
        engine.tick()
        self.assertEqual(engine.states[0].parsed_messages, 150)
        reader.buffers[1] += '\n테스터 (10:00:00) : 하나은행신규 매도'
        self.advance(engine, .5)
        self.assertEqual(engine.states[0].parsed_messages, 152)
        self.assertEqual(self.store.stats()['total'], 151)

    def test_full_content_same_second_and_rooms_are_distinct(self):
        prefix = '하나은행 ' + '가'*40
        rows = [message(content=prefix+' 매수'), message(content=prefix+' 매도'), message(room=ROOMS[1], content=prefix+' 매수')]
        self.assertEqual(self.store.insert(rows), 3)
        self.assertEqual(self.store.insert(rows), 0)

    def test_search_scope_conditions_two_characters_and_snapshot(self):
        self.store.insert([message(), message(content='국고26-2 매도', sender='하나은행담당자'), message(room=ROOMS[1], content='하나은행26-2 매도')])
        result = self.store.search('하나은행 채권 보여줘')
        self.assertEqual(result['total'], 2)
        self.assertEqual(self.store.search('하나은행', scope='sender')['total'], 1)
        self.assertEqual(self.store.search('하나')['total'], 2)
        self.assertEqual(self.store.search('막무가내 하나은행 매도')['total'], 1)
        self.store.insert([message(content='하나은행 새내역 매수', clock='10:00:00')])
        self.assertEqual(self.store.search('하나은행', snapshot=result['snapshot'])['total'], 2)
        self.assertEqual(self.store.search('하나은행')['total'], 3)
        self.assertEqual(self.store.search("' OR 1=1 --")['total'], 0)

    def test_pagination_keeps_frozen_snapshot(self):
        self.store.insert([message(content=f'하나은행{i} 매수') for i in range(110)])
        first = self.store.search('하나은행')
        self.assertEqual(len(first['rows']), 100)
        self.store.insert([message(content='하나은행신규 매수')])
        second = self.store.search('하나은행', page=2, snapshot=first['snapshot'])
        self.assertEqual(len(second['rows']), 10)
        self.assertFalse(set(r['id'] for r in first['rows']) & set(r['id'] for r in second['rows']))

    def test_midnight_multiline_and_series_hyphen(self):
        rows = parse_buffer('가 (23:59:59) : 하나은행26-1 매수\n조건 추가\n나 (00:00:01) : 국고26-2', ROOMS[0], dt.datetime(2026,10,14,0,0,2))
        self.assertEqual(rows[0]['message_at'], '2026-10-13 23:59:59')
        self.assertEqual(rows[1]['message_at'], '2026-10-14 00:00:01')
        self.assertIn('\n조건 추가', rows[0]['content'])
        self.assertEqual(rows[1]['side'], '')

    def test_csv_import_is_idempotent_and_source_unchanged(self):
        path = self.directory/'kbond_messages_2026-10-13.csv'
        def append(header=False):
            with path.open('w' if header else 'a', encoding='utf-8-sig', newline='') as out:
                writer = csv.writer(out)
                if header:
                    writer.writerow(['수집시간','송신자','종목','만기','구분','대화내용'])
                writer.writerow(['2026-10-13 10:00:00','보낸이','="하나은행26-1"','="27.09"','매수','하나은행 매수\n상세 조건'])
        append(True)
        before = path.read_bytes()
        self.assertEqual(self.store.import_csv(self.directory)['imported'], 1)
        self.assertEqual(self.store.import_csv(self.directory)['imported'], 0)
        self.assertEqual(path.read_bytes(), before)
        append()
        self.assertEqual(self.store.import_csv(self.directory)['imported'], 1)
        result = self.store.search('하나은행')
        self.assertEqual(result['total'], 2)
        self.assertEqual(result['rows'][0]['ticker'], '하나은행26-1')

    def test_api_validation_and_local_assets(self):
        client = create_app(self.store).test_client()
        self.assertEqual(client.get('/').status_code, 200)
        with client.get('/static/app.js') as response:
            self.assertEqual(response.status_code, 200)
        self.assertEqual(client.get('/api/search?page=bad').status_code, 400)
        self.assertEqual(client.get('/api/search?from=bad').status_code, 400)
        self.assertEqual(client.get('/api/search?from=2026-10-14&to=2026-10-13').status_code, 400)
        self.assertEqual(client.get('/api/status').json['rooms'][0]['status'], 'disabled')


if __name__ == '__main__':
    unittest.main(verbosity=2)
