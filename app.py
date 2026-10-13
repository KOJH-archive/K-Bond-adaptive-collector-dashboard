"""Local V3 server and collector in one process; V2 stays entirely separate."""
import argparse
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import threading
import time
import urllib.request
import json
import webbrowser

from flask import Flask, jsonify, render_template, request
from werkzeug.serving import make_server

from collector import Collector
from parsing import ROOMS
from storage import Store

BASE = Path(__file__).resolve().parent


def create_app(store, collector=None, import_result=None):
    app = Flask(__name__)
    start_wall, start_cpu = time.monotonic(), time.process_time()

    @app.get('/')
    def index():
        return render_template('index.html')

    @app.get('/api/health')
    def health():
        return jsonify(app='kbond-v3', data_path=str(store.path), collector=collector is not None)

    @app.get('/api/status')
    def status():
        rows = collector.snapshot() if collector else [dict(room=r, status='disabled', detail='조회 전용 모드', interval=0) for r in ROOMS]
        return jsonify(**store.stats(), rooms=rows, imports=import_result or {},
                       search_index='trigram' if store.fts else 'standard',
                       cpu_average=round(100*(time.process_time()-start_cpu)/max(1,time.monotonic()-start_wall), 2))

    @app.get('/api/search')
    def search():
        started = time.perf_counter()
        try:
            data = store.search(query=request.args.get('q', ''), room=request.args.get('room', ''),
                                side=request.args.get('side', ''), scope=request.args.get('scope', 'bond'),
                                date_from=request.args.get('from', ''), date_to=request.args.get('to', ''),
                                snapshot=request.args.get('snapshot'), page=request.args.get('page', 1))
        except (ValueError, OverflowError) as exc:
            return jsonify(error=str(exc)), 400
        data['elapsed_ms'] = round((time.perf_counter()-started)*1000, 1)
        return jsonify(data)

    @app.errorhandler(500)
    def failed(_error):
        return jsonify(error='저장소 조회에 실패했습니다. 잠시 후 다시 조회해 주세요.'), 500

    @app.after_request
    def local_headers(response):
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Content-Security-Policy'] = "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'"
        return response
    return app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=5001)
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--no-collect', action='store_true')
    parser.add_argument('--seconds', type=float, default=0, help='Bounded diagnostic run')
    parser.add_argument('--data-dir', type=Path, default=BASE/'data')
    parser.add_argument('--import-dir', type=Path, default=BASE.parent)
    args = parser.parse_args()
    args.data_dir.mkdir(parents=True, exist_ok=True)
    db_path = (args.data_dir/'kbond.sqlite3').resolve()
    url = f'http://127.0.0.1:{args.port}'
    # Detect an existing instance before starting a second collector.
    try:
        with urllib.request.urlopen(url+'/api/health', timeout=1) as response:
            existing = json.load(response)
        if existing.get('app') == 'kbond-v3' and existing.get('data_path') == str(db_path):
            print('이미 실행 중입니다: '+url)
            if not args.no_browser:
                webbrowser.open(url)
            return
    except (OSError, ValueError):
        pass
    logfile = args.data_dir/'engine.log'
    logging.basicConfig(level=logging.INFO, handlers=[RotatingFileHandler(logfile, maxBytes=1_000_000, backupCount=2, encoding='utf-8')])
    logging.getLogger('werkzeug').setLevel(logging.ERROR)
    store = Store(db_path)
    collector = None
    if not args.no_collect:
        from native import NativeReader
        collector = Collector(store, NativeReader())
    imports = {}
    app = create_app(store, collector, imports)
    # Bind before migration or collector start: concurrent launches cannot duplicate workers.
    server = make_server('127.0.0.1', args.port, app, threaded=True)
    try:
        imports.update(store.import_csv(args.import_dir))
        print(f'K-Bond V3: {url}\n기존 CSV 가져오기: {imports["imported"]}건')
        for error in imports['errors']:
            print('[가져오기 안내] '+error)
        print('이 창을 유지해 주세요. 종료하려면 Ctrl+C를 누르세요.')
        if collector:
            collector.start()
        if not args.no_browser:
            webbrowser.open(url)
        if args.seconds:
            timer = threading.Timer(args.seconds, server.shutdown)
            timer.daemon = True
            timer.start()
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if collector:
            collector.stop()
        server.server_close()


if __name__ == '__main__':
    main()
