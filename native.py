"""Read-only Windows text access with bounded timeouts and cached HWNDs."""
import ctypes
from ctypes import wintypes
import re
import sys
import time

from parsing import HEADER


class NativeReader:
    def __init__(self):
        if sys.platform != 'win32':
            raise RuntimeError('실시간 수집은 Windows에서 실행해 주세요.')
        self.api = ctypes.WinDLL('user32', use_last_error=True)
        self.callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        self.api.EnumWindows.argtypes = [self.callback, wintypes.LPARAM]
        self.api.EnumChildWindows.argtypes = [wintypes.HWND, self.callback, wintypes.LPARAM]
        self.api.IsWindow.argtypes = [wintypes.HWND]
        self.api.IsWindow.restype = wintypes.BOOL
        self.api.IsChild.argtypes = [wintypes.HWND, wintypes.HWND]
        self.api.IsChild.restype = wintypes.BOOL
        self.api.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        self.api.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        self.api.SendMessageTimeoutW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                                                wintypes.LPARAM, wintypes.UINT, wintypes.UINT,
                                                ctypes.POINTER(ctypes.c_size_t)]
        self.api.SendMessageTimeoutW.restype = ctypes.c_ssize_t
        self.roots = {}
        self.scanned_at = -float('inf')

    def valid(self, hwnd):
        return bool(hwnd and self.api.IsWindow(hwnd))

    def belongs(self, root, child):
        return root == child or bool(self.api.IsChild(root, child))

    def text(self, hwnd):
        if not self.valid(hwnd):
            raise OSError('채팅 창이 닫혔습니다.')
        size = ctypes.c_size_t()
        # SMTO_ABORTIFHUNG | SMTO_ERRORONEXIT; never wait indefinitely.
        if not self.api.SendMessageTimeoutW(hwnd, 0x000E, 0, 0, 0x22, 100, ctypes.byref(size)):
            raise TimeoutError('채팅 창의 응답을 기다리는 중입니다.')
        if size.value > 2_000_000:
            raise ValueError('대화 버퍼가 너무 큽니다. 창의 표시 내역을 줄여 주세요.')
        if not size.value:
            return ''
        buffer = ctypes.create_unicode_buffer(size.value + 1)
        result = ctypes.c_size_t()
        if not self.api.SendMessageTimeoutW(hwnd, 0x000D, len(buffer), ctypes.addressof(buffer),
                                           0x22, 100, ctypes.byref(result)):
            raise TimeoutError('대화 내용 읽기가 지연되고 있습니다.')
        return buffer.value

    def find_window(self, room):
        if time.monotonic() - self.scanned_at >= 2:
            roots = {}
            @self.callback
            def visit(hwnd, _):
                buffer = ctypes.create_unicode_buffer(1024)
                self.api.GetWindowTextW(hwnd, buffer, len(buffer))
                if '[채권]' in buffer.value:
                    roots[buffer.value] = hwnd
                return True
            self.api.EnumWindows(visit, 0)
            self.roots = roots
            self.scanned_at = time.monotonic()
        for title, hwnd in self.roots.items():
            if room in title and self.valid(hwnd):
                return hwnd
        return None

    def find_text_handle(self, root):
        candidates = []
        @self.callback
        def visit(hwnd, _):
            buffer = ctypes.create_unicode_buffer(256)
            self.api.GetClassNameW(hwnd, buffer, len(buffer))
            cls = buffer.value.lower()
            # Edits without a history header are excluded below.
            candidates.append((0 if 'richedit' in cls else 1 if 'edit' in cls else 2, hwnd))
            return len(candidates) < 256
        self.api.EnumChildWindows(root, visit, 0)
        candidates.append((2, root))
        best, best_score = None, (-1, -1, -1)
        deadline = time.monotonic() + 1.5
        for priority, hwnd in sorted(candidates):
            if time.monotonic() >= deadline:
                break
            try:
                text = self.text(hwnd)
            except (OSError, TimeoutError, ValueError):
                continue
            # Prefer a history control with actual message headers, not a clock widget.
            matches = [HEADER.match(line) for line in text.splitlines()]
            clocks = []
            for match in matches:
                if match:
                    h, m, s = map(int, match.group(2).split(':'))
                    if h < 24 and m < 60 and s < 60:
                        clocks.append(h*3600+m*60+s)
            # A larger but stale history must not outrank a newer chat buffer.
            score = (max(clocks, default=-1), 2-priority, len(clocks))
            if clocks and score > best_score:
                best, best_score = hwnd, score
        return best
