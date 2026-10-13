"""Read-only summary of the current archive plus a deterministic idle workload."""
import json
from pathlib import Path
import statistics
import tempfile
import time

from collector import Collector
from storage import Store
from test_v3 import FakeReader, NOW


def main():
    result = {}
    path = Path(__file__).resolve().parent/'data'/'kbond.sqlite3'
    if path.exists():
        store = Store(path)
        measurements = []
        for _ in range(20):
            started = time.perf_counter()
            response = store.search('하나은행 채권 보여줘')
            measurements.append((time.perf_counter()-started)*1000)
        result['archive'] = dict(**store.stats(), hana_matches=response['total'],
                                 search_median_ms=round(statistics.median(measurements), 2), fts=store.fts)
        with store.connect() as db:
            result['rooms'] = [dict(r) for r in db.execute('SELECT room,source,COUNT(*) count,MAX(message_at) last_message_time FROM messages GROUP BY room,source')]
    with tempfile.TemporaryDirectory() as directory:
        store = Store(Path(directory)/'metrics.sqlite3')
        elapsed = [0]
        engine = Collector(store, FakeReader(), clock=lambda: elapsed[0], wall=lambda: NOW)
        engine.tick()
        for _ in range(120):
            elapsed[0] += .5
            engine.tick()
        state = engine.states[0]
        result['idle_60s_per_room'] = dict(fixed_half_second_reads=121, adaptive_reads=state.reads,
                                         parse_count=state.parses,
                                         read_reduction_percent=round((1-state.reads/121)*100,1))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
