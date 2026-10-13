"""One sequential collector; each room owns its schedule and failure state."""
from dataclasses import dataclass
import datetime as dt
import re
import threading
import time

from parsing import ROOMS, parse_buffer

HEADER_START = re.compile(r'^[ \t]*[^\r\n]*?\([ \t]*\d{1,2}:\d{1,2}:\d{1,2}[ \t]*\)[ \t]*:', re.M)


@dataclass
class RoomState:
    room: str
    root: int | None = None
    handle: int | None = None
    next_due: float = 0
    interval: float = 0.5
    idle: int = 0
    previous: str | None = None
    status: str = 'waiting'
    detail: str = '연결 확인 중'
    last_check: str = ''
    last_message: str = ''
    inserted: int = 0
    reads: int = 0
    parses: int = 0
    next_scan: float = 0
    tail_start: int = 0
    parsed_messages: int = 0


class Collector:
    def __init__(self, store, reader, clock=time.monotonic, wall=dt.datetime.now):
        self.store, self.reader, self.clock, self.wall = store, reader, clock, wall
        self.states = [RoomState(room) for room in ROOMS]
        self.stop_event = threading.Event()
        self.thread = None
        self.lock = threading.Lock()
        self.published = []
        self.publish()

    def publish(self):
        rows = [dict(room=s.room, status=s.status, detail=s.detail, interval=s.interval,
                     last_check=s.last_check, last_message=s.last_message, inserted=s.inserted,
                     reads=s.reads, parses=s.parses, parsed_messages=s.parsed_messages) for s in self.states]
        with self.lock:
            self.published = rows

    def snapshot(self):
        with self.lock:
            return [dict(row) for row in self.published]

    def tick(self):
        for state in self.states:
            if self.clock() < state.next_due:
                continue
            try:
                self.poll(state)
            except Exception as exc:
                # Bounded native timeouts plus this boundary prevent one room blocking the other.
                state.root = state.handle = None
                state.status, state.detail = 'retrying', str(exc)[:180]
                state.interval = 10
            finally:
                state.last_check = self.wall().strftime('%Y-%m-%d %H:%M:%S')
                state.next_due = self.clock() + state.interval
        self.publish()

    def poll(self, state):
        if not self.reader.valid(state.root):
            state.root = self.reader.find_window(state.room)
            state.handle = None
            if not state.root:
                state.status, state.detail, state.interval = 'offline', '방을 열면 자동 연결됩니다.', 10
                return
        if not self.reader.valid(state.handle) or not self.reader.belongs(state.root, state.handle):
            state.handle = self.reader.find_text_handle(state.root)
            state.next_scan = self.clock() + 60
            if not state.handle:
                state.status, state.detail, state.interval = 'waiting', '대화 내용을 찾는 중입니다.', 10
                return
        elif self.clock() >= state.next_scan:
            # Some messengers leave the old control alive when changing tabs.
            found = self.reader.find_text_handle(state.root)
            if found:
                state.handle = found
            state.next_scan = self.clock() + 60
        text = self.reader.text(state.handle)
        state.reads += 1
        state.status, state.detail = 'connected', '수집 중'
        changed = text != state.previous
        if changed:
            # Common messenger behavior is an append-only history. Revisit its last
            # message (it may have a continued body) and parse just the appended tail.
            offset = state.tail_start if state.previous and text.startswith(state.previous) else 0
            segment = text[offset:]
            rows = parse_buffer(segment, state.room, self.wall())
            state.parses += 1
            state.parsed_messages += len(rows)
            added = self.store.insert(rows)
            # Advance only after a successful durable DB write so failures are retried.
            state.previous = text
            for match in HEADER_START.finditer(segment):
                state.tail_start = offset + match.start()
            state.inserted += added
            if added:
                state.last_message = self.wall().strftime('%Y-%m-%d %H:%M:%S')
                state.idle, state.interval = 0, 0.5
                return
        state.idle += 1
        state.interval = 0.5 if state.idle < 4 else 1 if state.idle < 8 else 2 if state.idle < 12 else 5
        if not text:
            state.handle = None
            state.status, state.detail, state.interval = 'waiting', '대화가 표시되면 자동 연결됩니다.', 10

    def run(self):
        while not self.stop_event.is_set():
            self.tick()
            delay = max(0.05, min(state.next_due for state in self.states) - self.clock())
            self.stop_event.wait(delay)

    def start(self):
        self.thread = threading.Thread(target=self.run, name='kbond-collector', daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=6)
