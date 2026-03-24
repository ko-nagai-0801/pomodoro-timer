#!/usr/bin/env python3
"""Pomodoro Timer v3 – Pure AppKit transparent floating overlay"""

__version__ = '3.0.0'

import time, json, os, tempfile, subprocess, csv, shutil, math, threading, logging
from datetime import date, timedelta
import objc
from AppKit import (
    NSApplication, NSPanel, NSView, NSColor, NSBezierPath,
    NSFont, NSFontAttributeName, NSForegroundColorAttributeName,
    NSAttributedString, NSParagraphStyleAttributeName,
    NSMutableParagraphStyle, NSCenterTextAlignment,
    NSShadow, NSShadowAttributeName,
    NSMenu, NSMenuItem, NSTrackingArea,
    NSAlert, NSTextField, NSStatusBar, NSVariableStatusItemLength,
    NSBackingStoreBuffered, NSFloatingWindowLevel,
    NSMakeRect, NSMakePoint, NSMakeSize,
    NSTrackingMouseEnteredAndExited, NSTrackingActiveAlways,
    NSApplicationActivationPolicyAccessory,
    NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorStationary,
    NSScreen, NSSound, NSWorkspace, NSEvent, NSSavePanel,
    NSCursor,
)
from Foundation import NSObject, NSTimer

# ── Optional frameworks ───────────────────────────────────────────────────────
try:
    from UserNotifications import (
        UNUserNotificationCenter, UNMutableNotificationContent,
        UNNotificationRequest,
        UNAuthorizationOptionAlert, UNAuthorizationOptionSound,
    )
    HAS_UN = True
except ImportError:
    HAS_UN = False

try:
    from ServiceManagement import SMAppService
    HAS_SM = True
except ImportError:
    HAS_SM = False

try:
    from AppKit import NSEventMaskKeyDown as _KEY_DOWN_MASK
except ImportError:
    _KEY_DOWN_MASK = 1024

CMD_FLAG = 1 << 20
SHF_FLAG = 1 << 17

# ── Paths (#5: ~/Library/Application Support) ─────────────────────────────────
_APP_SUPPORT   = os.path.expanduser('~/Library/Application Support/PomodoroTimer')
CONFIG_PATH    = os.path.join(_APP_SUPPORT, 'config.json')
BACKUP_PATH    = CONFIG_PATH + '.backup'
HISTORY_PATH   = os.path.join(_APP_SUPPORT, 'history.json')
LOG_PATH       = os.path.join(_APP_SUPPORT, 'pomodoro.log')
# Legacy paths for migration
_LEGACY_BASE   = os.path.dirname(os.path.abspath(__file__))
_LEGACY_CONFIG = os.path.join(_LEGACY_BASE, 'config.json')
_LEGACY_HIST   = os.path.expanduser('~/.pomodoro-timer/history.json')

W  = 140
CX = CY = 70.0
R  = 55.0
COORDS_VERSION = 2

SHORTCUTS_HELP = [
    'Cmd+Shift+P: 一時停止 / 再開',
    'Cmd+Shift+N: スキップ',
    'Cmd+Shift+R: リセット',
]

# ── Themes ────────────────────────────────────────────────────────────────────
THEMES = {
    'blue':    dict(focus='#6BA3E0', break_='#6BC4BA', paused='#A090C8',
                    base='#2D3748', mode='#4A6A7A', label='ブルー'),
    'classic': dict(focus='#E07070', break_='#5CC4BC', paused='#E8C84A',
                    base='#3A3A3A', mode='#787878', label='クラシック'),
    'purple':  dict(focus='#9B7FC8', break_='#5CBCB0', paused='#E0A060',
                    base='#3A3050', mode='#786890', label='パープル'),
    'mono':    dict(focus='#B0B8C0', break_='#909898', paused='#C8CCD0',
                    base='#485060', mode='#607080', label='モノクロ'),
    'auto':    dict(focus='#6BA3E0', break_='#6BC4BA', paused='#A090C8',
                    base='#2D3748', mode='#4A6A7A', label='自動 (システム連動)'),
}

SOUNDS           = ['Glass', 'Tink', 'Bell', 'Blow', 'Bottle', 'Frog',
                    'Funk', 'Morse', 'Pop', 'Purr', 'Sosumi', 'Submarine', 'なし']
WORK_OPTIONS     = [15*60, 25*60, 30*60, 45*60, 50*60, 60*60, 90*60]
BREAK_OPTIONS    = [5*60, 10*60, 15*60, 20*60]
OPACITY_OPTIONS  = [(1.0, '100%'), (0.6, '60%'), (0.3, '30%')]
AUTO_START_MODES = ['手動', 'フェーズ自動', '完全自動']

# ── Logging (#23) ─────────────────────────────────────────────────────────────
def _setup_log():
    os.makedirs(_APP_SUPPORT, exist_ok=True)
    logging.basicConfig(
        filename=LOG_PATH, level=logging.WARNING,
        format='%(asctime)s %(levelname)s %(message)s')

def _log(msg, level=logging.WARNING):
    try:
        logging.log(level, msg)
        print(f'[{logging.getLevelName(level)}] {msg}', flush=True)
    except Exception:
        pass


# ── Color helpers ─────────────────────────────────────────────────────────────
def ns(h: str, a: float = 1.0) -> NSColor:
    r = int(h[1:3], 16) / 255
    g = int(h[3:5], 16) / 255
    b = int(h[5:7], 16) / 255
    return NSColor.colorWithSRGBRed_green_blue_alpha_(r, g, b, a)


def _lerp_hex(a: str, b: str, t: float) -> str:
    ar, ag, ab = int(a[1:3], 16), int(a[3:5], 16), int(a[5:7], 16)
    br, bg, bb = int(b[1:3], 16), int(b[3:5], 16), int(b[5:7], 16)
    return '#{:02X}{:02X}{:02X}'.format(
        int(ar + (br - ar) * t), int(ag + (bg - ag) * t), int(ab + (bb - ab) * t))


# #14: cache _is_dark() to avoid repeated AppKit calls in drawRect_
_dark_cache: bool = False
_dark_cache_t: float = 0.0

def _is_dark() -> bool:
    global _dark_cache, _dark_cache_t
    now = time.monotonic()
    if now - _dark_cache_t < 5.0:
        return _dark_cache
    try:
        name = str(NSApplication.sharedApplication().effectiveAppearance().name())
        _dark_cache = 'Dark' in name
    except Exception:
        _dark_cache = False
    _dark_cache_t = now
    return _dark_cache


def _resolve_theme(key: str) -> dict:
    if key == 'auto':
        return THEMES['mono' if _is_dark() else 'blue']
    return THEMES.get(key, THEMES['blue'])


# ── History (#4: async I/O, #24: trim old entries) ────────────────────────────
class TimerHistory:
    _MAX_AGE_DAYS = 365

    def __init__(self):
        os.makedirs(_APP_SUPPORT, exist_ok=True)
        self._d: dict = {}
        self._lock = threading.Lock()
        self._load()

    def _load(self):
        for path in [HISTORY_PATH, _LEGACY_HIST]:
            try:
                with open(path) as f:
                    self._d = json.load(f)
                if path == _LEGACY_HIST:
                    self._save_bg()
                    _log(f'Migrated history from {path}', level=logging.INFO)
                return
            except FileNotFoundError:
                continue
            except json.JSONDecodeError as e:
                _log(f'history.json parse error: {e}')

    def _trim(self):
        cutoff = (date.today() - timedelta(days=self._MAX_AGE_DAYS)).isoformat()
        for k in list(self._d.keys()):
            if k < cutoff:
                e = self._d[k]
                if 'times' in e:
                    del e['times']   # keep count, drop timestamps

    def _save_sync(self):
        try:
            with self._lock:
                self._trim()
                snapshot = dict(self._d)
            fd, tmp = tempfile.mkstemp(dir=_APP_SUPPORT)
            with os.fdopen(fd, 'w') as f:
                json.dump(snapshot, f)
            os.replace(tmp, HISTORY_PATH)
        except Exception as e:
            _log(f'history save error: {e}')

    def _save_bg(self):
        threading.Thread(target=self._save_sync, daemon=True).start()

    def record(self, memo: str = ''):
        k = date.today().isoformat()
        with self._lock:
            e = self._d.setdefault(k, {'count': 0, 'times': []})
            e['count'] += 1
            entry = {'t': int(time.time()), 'memo': memo} if memo else int(time.time())
            e['times'].append(entry)
        self._save_bg()

    def today_count(self) -> int:
        return self._d.get(date.today().isoformat(), {}).get('count', 0)

    def week_count(self) -> int:
        today = date.today()
        return sum(
            self._d.get((today - timedelta(days=i)).isoformat(), {}).get('count', 0)
            for i in range(7)
        )

    def export_csv(self, path: str):
        with open(path, 'w', newline='', encoding='utf-8') as f:
            w = csv.writer(f)
            w.writerow(['日付', 'ポモドーロ数'])
            for d in sorted(self._d):
                w.writerow([d, self._d[d].get('count', 0)])


# ── Timer State ───────────────────────────────────────────────────────────────
class TimerState:
    IDLE, RUNNING, PAUSED, FINISHED = 'idle', 'running', 'paused', 'finished'
    SET_SIZE   = 4
    LONG_BREAK = 15 * 60

    def __init__(self):
        self._load_config()
        self.history      = TimerHistory()
        self.state        = self.IDLE
        self.is_focus     = True
        self.total_secs   = float(self.work_duration)
        self.remaining    = float(self.work_duration)
        self._paused_rem  = float(self.work_duration)
        self._mono_start  = 0.0
        self._flash_t     = 0.0
        self._trans_t     = 0.0
        self.hover        = False
        self._sleep_paused = False
        self.current_memo = ''

    # ── Config ────────────────────────────────────────────────────────────────
    def _load_config(self):
        os.makedirs(_APP_SUPPORT, exist_ok=True)
        d = dict(work_duration=25*60, break_duration=5*60,
                 window_x=None, window_y=None, coords_version=0,
                 pomodoro_count=0, last_date='',
                 auto_start=0, color_theme='blue', opacity=1.0,
                 notify_sound='Glass', auto_launch=False, always_dots=False)

        # Migrate legacy config
        if not os.path.exists(CONFIG_PATH) and os.path.exists(_LEGACY_CONFIG):
            try:
                shutil.copy2(_LEGACY_CONFIG, CONFIG_PATH)
                _log(f'Migrated config from {_LEGACY_CONFIG}', level=logging.INFO)
            except Exception:
                pass

        if os.path.exists(CONFIG_PATH):
            try:
                shutil.copy2(CONFIG_PATH, BACKUP_PATH)
            except Exception:
                pass

        try:
            with open(CONFIG_PATH) as f:
                d.update(json.load(f))
        except FileNotFoundError:
            pass
        except json.JSONDecodeError as e:
            _log(f'config.json decode error: {e} – using backup')
            try:
                with open(BACKUP_PATH) as f:
                    d.update(json.load(f))
            except Exception:
                pass
        except Exception as e:
            _log(f'config load error: {e}')

        self.work_duration  = max(60, min(90*60, int(d['work_duration'])))
        self.break_duration = max(60, min(30*60, int(d['break_duration'])))
        self.cfg_x          = d['window_x']
        self.cfg_y          = d['window_y']
        self.coords_version = int(d.get('coords_version', 0))

        today = date.today().isoformat()
        last  = d.get('last_date', '')
        self.pomodoro_count = 0 if last != today else int(d.get('pomodoro_count', 0))
        self.last_date      = today

        raw = d.get('auto_start', 0)
        if isinstance(raw, bool):
            raw = 2 if raw else 0
        self.auto_start = max(0, min(2, int(raw)))

        self.color_theme = d.get('color_theme', 'blue')
        if self.color_theme not in THEMES:
            self.color_theme = 'blue'

        self.opacity      = max(0.1, min(1.0, float(d.get('opacity', 1.0))))
        self.notify_sound = d.get('notify_sound', 'Glass')
        if self.notify_sound not in SOUNDS:
            self.notify_sound = 'Glass'
        self.auto_launch  = bool(d.get('auto_launch', False))
        self.always_dots  = bool(d.get('always_dots', False))

    def save(self, wx=None, wy=None):
        data = dict(work_duration=self.work_duration, break_duration=self.break_duration,
                    window_x=wx, window_y=wy, coords_version=COORDS_VERSION,
                    pomodoro_count=self.pomodoro_count, last_date=self.last_date,
                    auto_start=self.auto_start, color_theme=self.color_theme,
                    opacity=self.opacity, notify_sound=self.notify_sound,
                    auto_launch=self.auto_launch, always_dots=self.always_dots)
        def _do():
            try:
                fd, tmp = tempfile.mkstemp(dir=_APP_SUPPORT)
                with os.fdopen(fd, 'w') as f:
                    json.dump(data, f)
                os.replace(tmp, CONFIG_PATH)
            except Exception as e:
                _log(f'config save error: {e}')
        threading.Thread(target=_do, daemon=True).start()

    # ── Theme helpers ─────────────────────────────────────────────────────────
    @property
    def theme(self) -> dict:
        return _resolve_theme(self.color_theme)

    @property
    def accent_hex(self) -> str:
        t = self.theme
        if self.state == self.PAUSED:
            return t['paused']
        # #29: warm color shift in last 5 min of focus
        if self.is_focus and self.state == self.RUNNING and 0 < self.remaining < 5*60:
            ratio = (1.0 - self.remaining / (5*60)) * 0.6
            return _lerp_hex(t['focus'], '#E8A060', ratio)
        return t['focus'] if self.is_focus else t['break_']

    def calc_break(self) -> int:
        if self.pomodoro_count > 0 and self.pomodoro_count % self.SET_SIZE == 0:
            return self.LONG_BREAK
        return self.break_duration

    # ── Notifications ─────────────────────────────────────────────────────────
    def _notify(self, title: str = 'Pomodoro Timer', body: str = ''):
        if not body:
            body = '休憩時間です！' if self.is_focus else '集中を再開しましょう！'
        if self.notify_sound != 'なし':
            try:
                snd = NSSound.soundNamed_(self.notify_sound)
                if snd:
                    snd.play()
            except Exception:
                pass
        if HAS_UN:
            try:
                content = UNMutableNotificationContent.alloc().init()
                content.setTitle_(title)
                content.setBody_(body)
                req = UNNotificationRequest.requestWithIdentifier_content_trigger_(
                    f'pd_{int(time.time())}', content, None)
                UNUserNotificationCenter.currentNotificationCenter() \
                    .addNotificationRequest_withCompletionHandler_(
                        req,
                        lambda granted, err: _log(f'UNNotification: {err}') if err else None)
                return
            except Exception as e:
                _log(f'UNNotification error: {e}')
        try:
            subprocess.Popen(['osascript', '-e',
                f'display notification "{body}" with title "{title}"'])
        except Exception:
            pass

    # ── Timer logic (#1: update() called from tick_, not drawRect_) ───────────
    def update(self):
        if self.state == self.RUNNING:
            elapsed = time.monotonic() - self._mono_start
            self.remaining = max(0.0, self._paused_rem - elapsed)
            if self.remaining <= 0.001:
                self.remaining = 0.0
                self.state     = self.FINISHED
                self._flash_t  = time.monotonic()
                if self.is_focus:
                    self.pomodoro_count += 1
                    self.history.record(self.current_memo)
                    self.current_memo = ''
                self.save()
                self._notify()
        # Auto-advance after 3s flash (#20: countdown visible)
        if self.state == self.FINISHED and self.auto_start >= 1:
            if time.monotonic() - self._flash_t >= 3.0:
                self._advance_phase()

    def flash_visible(self) -> bool:
        if self.state == self.FINISHED:
            elapsed = time.monotonic() - self._flash_t
            if elapsed > 3.0:
                return True
            return elapsed % 0.5 < 0.3
        if self.state == self.PAUSED:
            return True   # always "visible"; alpha handled by paused_alpha()
        return True

    def paused_alpha(self) -> float:
        """#26: smooth sin-wave fade for PAUSED state."""
        if self.state == self.PAUSED:
            return 0.35 + 0.65 * (0.5 + 0.5 * math.sin(time.monotonic() * math.pi * 1.2))
        return 1.0

    def trans_alpha(self) -> float:
        """#16: arc fade-in with smoothstep easing over 0.6s."""
        if self.state == self.RUNNING:
            p = min(1.0, (time.monotonic() - self._trans_t) / 0.6)
            return p * p * (3.0 - 2.0 * p)
        return 1.0

    def auto_cd_remaining(self) -> float:
        """#20: seconds left before auto-advance (3s window)."""
        if self.state == self.FINISHED and self.auto_start >= 1:
            return max(0.0, 3.0 - (time.monotonic() - self._flash_t))
        return 0.0

    def arc_pulse_width(self) -> float:
        """#28: breathing line width 3.0↔3.5 at 0.5s period."""
        return 3.0 + 0.25 * (0.5 + 0.5 * math.sin(time.monotonic() * math.pi * 2.0))

    def _do_start(self):
        self._trans_t    = time.monotonic()
        self._mono_start = time.monotonic()
        self.state       = self.RUNNING

    def _advance_phase(self):
        self.is_focus    = not self.is_focus
        dur              = float(self.work_duration if self.is_focus else self.calc_break())
        self.total_secs  = dur
        self._paused_rem = dur
        self.remaining   = dur
        if self.auto_start >= 2:
            self._do_start()
        else:
            self.state = self.IDLE

    def handle_click(self):
        if   self.state == self.IDLE:
            self._do_start()
        elif self.state == self.RUNNING:
            elapsed          = time.monotonic() - self._mono_start
            self._paused_rem = max(0.0, self._paused_rem - elapsed)
            self.remaining   = self._paused_rem
            self.state       = self.PAUSED
        elif self.state == self.PAUSED:
            self._mono_start = time.monotonic()
            self.state       = self.RUNNING
        elif self.state == self.FINISHED:
            self._advance_phase()

    def reset(self):
        self.state        = self.IDLE
        self.is_focus     = True
        self.total_secs   = float(self.work_duration)
        self._paused_rem  = float(self.work_duration)
        self.remaining    = float(self.work_duration)
        self.current_memo = ''

    def skip(self):
        if self.is_focus:
            self.pomodoro_count += 1
        self.is_focus    = not self.is_focus
        dur              = float(self.work_duration if self.is_focus else self.calc_break())
        self.total_secs  = dur
        self._paused_rem = dur
        self.remaining   = dur
        self.state       = self.IDLE
        self.save()

    def sleep_pause(self):
        if self.state == self.RUNNING:
            elapsed          = time.monotonic() - self._mono_start
            self._paused_rem = max(0.0, self._paused_rem - elapsed)
            self.remaining   = self._paused_rem
            self.state       = self.PAUSED
            self._sleep_paused = True


# ── Timer View ────────────────────────────────────────────────────────────────
class TimerView(NSView):

    def isOpaque(self):
        return False

    def initWithFrame_(self, frame):
        self = objc.super(TimerView, self).initWithFrame_(frame)
        if self is not None:
            self.ts       = None
            self._press   = False
            self._moved   = False
            self._accum_d = 0.0
            # #15: cached drawing resources (initialized once)
            self._font_time  = None
            self._font_small = None
            self._shadow     = None
            self._para       = None
        return self

    def _init_resources(self):
        if self._font_time is not None:
            return
        self._font_time  = (NSFont.fontWithName_size_('Menlo-Bold', 22.0) or
                            NSFont.boldSystemFontOfSize_(22.0))
        self._font_small = (NSFont.fontWithName_size_('Menlo', 9.0) or
                            NSFont.systemFontOfSize_(9.0))
        shadow = NSShadow.alloc().init()
        shadow.setShadowColor_(NSColor.colorWithWhite_alpha_(0.0, 0.7))
        shadow.setShadowOffset_(NSMakeSize(0, -1))
        shadow.setShadowBlurRadius_(4.0)
        self._shadow = shadow
        para = NSMutableParagraphStyle.alloc().init()
        para.setAlignment_(NSCenterTextAlignment)
        self._para = para

    # ── Drawing ───────────────────────────────────────────────────────────────
    def drawRect_(self, rect):
        NSColor.clearColor().set()
        NSBezierPath.fillRect_(self.bounds())

        ts = self.ts
        if ts is None:
            return

        self._init_resources()

        t      = ts.theme
        acc    = ts.accent_hex
        center = NSMakePoint(CX, CY)
        ta     = ts.trans_alpha()
        pa     = ts.paused_alpha()
        oval   = NSMakeRect(CX - R, CY - R, R * 2, R * 2)

        # #6: contrast boost in light mode
        base_alpha = 0.9 if _is_dark() else 1.0

        # ── Base ring ─────────────────────────────────────────────────────────
        glow = NSBezierPath.bezierPathWithOvalInRect_(oval)
        glow.setLineWidth_(4.0)
        ns(t['base'], 0.25).set()
        glow.stroke()
        ring = NSBezierPath.bezierPathWithOvalInRect_(oval)
        ring.setLineWidth_(2.0)
        ns(t['base'], base_alpha).set()
        ring.stroke()

        # ── Progress arc ──────────────────────────────────────────────────────
        if ts.state == ts.IDLE:
            idle = NSBezierPath.bezierPath()
            idle.appendBezierPathWithArcWithCenter_radius_startAngle_endAngle_clockwise_(
                center, R, 90.0, 90.0 - 359.9, True)
            idle.setLineWidth_(2.0)
            ns(acc, 0.18).set()
            idle.stroke()
        elif ts.total_secs > 0:
            ratio = ts.remaining / ts.total_secs
            vis   = ts.flash_visible()
            if ratio > 0.001 and vis:
                total_deg  = ratio * 360.0
                N          = 16
                step       = total_deg / N
                lw         = ts.arc_pulse_width()    # #28 breathing
                arc_alpha  = pa if ts.state == ts.PAUSED else 1.0
                for i in range(N):
                    seg_s = 90.0 - i * step
                    seg_e = seg_s - step - (1.0 if i < N - 1 else 0)  # #27 overlap
                    alpha = (1.0 - (i / N) * 0.65) * ta * arc_alpha
                    seg = NSBezierPath.bezierPath()
                    seg.appendBezierPathWithArcWithCenter_radius_startAngle_endAngle_clockwise_(
                        center, R, seg_s, seg_e, True)
                    seg.setLineWidth_(lw)
                    ns(acc, alpha).set()
                    seg.stroke()

        # ── Completion pulse ring ──────────────────────────────────────────────
        if ts.state == ts.FINISHED and ts.flash_visible():
            pulse = NSBezierPath.bezierPathWithOvalInRect_(oval)
            pulse.setLineWidth_(4.0)
            ns(acc, 0.7).set()
            pulse.stroke()

        # ── Time text ─────────────────────────────────────────────────────────
        m, s = divmod(int(ts.remaining), 60)
        t_alpha = 0.95
        if ts.state == ts.PAUSED:
            t_alpha *= pa
        elif ts.state == ts.FINISHED and not ts.flash_visible():
            t_alpha = 0.0
        attrs = {
            NSFontAttributeName:            self._font_time,
            NSForegroundColorAttributeName: ns(acc, t_alpha),
            NSShadowAttributeName:          self._shadow,
        }
        ns_str = NSAttributedString.alloc().initWithString_attributes_(f'{m:02d}:{s:02d}', attrs)
        sz = ns_str.size()
        ns_str.drawAtPoint_(NSMakePoint(CX - sz.width / 2, CY - sz.height / 2 + 1))

        # ── Mode / status label ───────────────────────────────────────────────
        label       = None
        label_alpha = 0.85
        if ts.state == ts.RUNNING:
            mode  = '集中' if ts.is_focus else '休憩'
            done  = ts.pomodoro_count % ts.SET_SIZE
            total = ts.SET_SIZE
            # #17: hover shows action hint; otherwise show #18 (N/total format)
            if ts.hover:
                label = f'{"集中" if ts.is_focus else "休憩"} — 一時停止'
            else:
                label = f'{mode} ({done}/{total})' if ts.is_focus else mode
        elif ts.state == ts.PAUSED:
            label       = f'⏸ {"集中" if ts.is_focus else "休憩"}'
            label_alpha = pa * 0.85
        elif ts.state == ts.FINISHED:
            # #8: clear FINISHED indicator
            label = '✓ 完了 — クリックで次へ'
        elif ts.state == ts.IDLE and ts.hover:
            mins  = (ts.work_duration if ts.is_focus else ts.break_duration) // 60
            label = f'{"集中" if ts.is_focus else "休憩"} {mins}分 — クリックで開始'

        if label:
            la = {NSFontAttributeName: self._font_small,
                  NSForegroundColorAttributeName: ns(t['mode'], label_alpha),
                  NSParagraphStyleAttributeName:  self._para}
            ls  = NSAttributedString.alloc().initWithString_attributes_(label, la)
            lsz = ls.size()
            ls.drawAtPoint_(NSMakePoint(CX - lsz.width / 2, CY - R + 6))

        # #20: auto-start countdown label
        cd = ts.auto_cd_remaining()
        if cd > 0:
            cd_text = f'自動開始まで {math.ceil(cd)}秒'
            cd_attr = {NSFontAttributeName: self._font_small,
                       NSForegroundColorAttributeName: ns(t['mode'], 0.7),
                       NSParagraphStyleAttributeName:  self._para}
            cd_str = NSAttributedString.alloc().initWithString_attributes_(cd_text, cd_attr)
            csz = cd_str.size()
            cd_str.drawAtPoint_(NSMakePoint(CX - csz.width / 2, CY - R + 20))

        # ── Pomodoro dots (#7 fill/stroke, #19 layout, #30 always option) ────
        if ts.hover or ts.always_dots:
            n     = ts.SET_SIZE
            done  = ts.pomodoro_count % n
            set_n = ts.pomodoro_count // n + 1

            # #19: improved layout — dots centered, set label above
            base_y = CY - R + 22

            if set_n > 1:
                sl = {NSFontAttributeName: self._font_small,
                      NSForegroundColorAttributeName: ns(t['mode'], 0.6),
                      NSParagraphStyleAttributeName:  self._para}
                ss  = NSAttributedString.alloc().initWithString_attributes_(f'Set {set_n}', sl)
                ssz = ss.size()
                ss.drawAtPoint_(NSMakePoint(CX - ssz.width / 2, base_y + 10))

            spacing = 12.0
            ox = CX - (n - 1) * spacing / 2
            for i in range(n):
                dr    = 3.5
                dx    = ox + i * spacing
                drect = NSMakeRect(dx - dr, base_y - dr, dr * 2, dr * 2)
                dot   = NSBezierPath.bezierPathWithOvalInRect_(drect)
                if i < done:
                    # #7: completed — filled solid circle
                    ns(acc, 0.9).set()
                    dot.fill()
                else:
                    # #7: incomplete — outline only, thinner stroke
                    ns(t['base'], 0.5).set()
                    dot.setLineWidth_(1.0)
                    dot.stroke()

    # ── Mouse events ──────────────────────────────────────────────────────────
    def acceptsFirstMouse_(self, event):
        return True

    def mouseDown_(self, event):
        self._press   = True
        self._moved   = False
        self._accum_d = 0.0

    def mouseDragged_(self, event):
        if not self._press:
            return
        dx =  event.deltaX()
        dy = -event.deltaY()
        self._accum_d += math.sqrt(dx * dx + dy * dy)
        if self._accum_d > 10:
            self._moved = True
        if self._moved:
            f = self.window().frame()
            f.origin.x += dx
            f.origin.y += dy
            self.window().setFrame_display_(f, True)

    def mouseUp_(self, event):
        if not self._moved and self.ts:
            self.ts.handle_click()
            self.setNeedsDisplay_(True)
        elif self._moved and self.ts:
            f = self.window().frame()
            self.ts.save(int(f.origin.x), int(f.origin.y))
        self._press   = False
        self._moved   = False
        self._accum_d = 0.0

    def rightMouseDown_(self, event):
        if not self.ts:
            return
        NSMenu.popUpContextMenu_withEvent_forView_(self._build_menu(), event, self)

    # #31: pointer cursor on hover
    def resetCursorRects(self):
        self.addCursorRect_cursor_(self.bounds(), NSCursor.pointingHandCursor())

    # ── Menu builder ──────────────────────────────────────────────────────────
    def _build_menu(self) -> NSMenu:
        ts   = self.ts
        menu = NSMenu.alloc().init()

        def item(title, sel, enabled=True):
            it = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, sel, '')
            it.setTarget_(self)
            if not enabled:
                it.setEnabled_(False)
            menu.addItem_(it)
            return it

        item('リセット', 'menuReset:')
        item('スキップ', 'menuSkip:')
        # #34: session memo
        item('セッションメモを入力…', 'menuSetMemo:')
        menu.addItem_(NSMenuItem.separatorItem())

        # #33: note if running that changes apply next session
        note = ' (次回から)' if ts.state == ts.RUNNING else ''

        # Work time submenu
        wsub = NSMenu.alloc().init()
        for v in WORK_OPTIONS:
            mark = '● ' if v == ts.work_duration else '  '
            wi = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                mark + f'{v//60}分{note}', 'menuSetWork:', '')
            wi.setTarget_(self); wi.setRepresentedObject_(str(v))
            wsub.addItem_(wi)
        wi_top = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            f'作業時間: {ts.work_duration//60}分', None, '')
        wi_top.setSubmenu_(wsub); menu.addItem_(wi_top)

        # Break time submenu
        bsub = NSMenu.alloc().init()
        for v in BREAK_OPTIONS:
            mark = '● ' if v == ts.break_duration else '  '
            bi = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                mark + f'{v//60}分{note}', 'menuSetBreak:', '')
            bi.setTarget_(self); bi.setRepresentedObject_(str(v))
            bsub.addItem_(bi)
        bi_top = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            f'休憩時間: {ts.break_duration//60}分', None, '')
        bi_top.setSubmenu_(bsub); menu.addItem_(bi_top)

        menu.addItem_(NSMenuItem.separatorItem())

        # Color theme
        csub = NSMenu.alloc().init()
        for key, th in THEMES.items():
            mark = '● ' if key == ts.color_theme else '  '
            ci = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                mark + th['label'], 'menuSetTheme:', '')
            ci.setTarget_(self); ci.setRepresentedObject_(key)
            csub.addItem_(ci)
        ci_top = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            'カラーテーマ', None, '')
        ci_top.setSubmenu_(csub); menu.addItem_(ci_top)

        # Opacity
        osub = NSMenu.alloc().init()
        for v, lbl in OPACITY_OPTIONS:
            mark = '● ' if abs(v - ts.opacity) < 0.05 else '  '
            oi = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                mark + lbl, 'menuSetOpacity:', '')
            oi.setTarget_(self); oi.setRepresentedObject_(str(v))
            osub.addItem_(oi)
        oi_top = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_('透明度', None, '')
        oi_top.setSubmenu_(osub); menu.addItem_(oi_top)

        # Notification sound
        nsub = NSMenu.alloc().init()
        for snd in SOUNDS:
            mark = '● ' if snd == ts.notify_sound else '  '
            ni = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                mark + snd, 'menuSetSound:', '')
            ni.setTarget_(self); ni.setRepresentedObject_(snd)
            nsub.addItem_(ni)
        ni_top = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            f'通知音: {ts.notify_sound}', None, '')
        ni_top.setSubmenu_(nsub); menu.addItem_(ni_top)

        menu.addItem_(NSMenuItem.separatorItem())

        # Auto-start
        asub = NSMenu.alloc().init()
        for i, lbl in enumerate(AUTO_START_MODES):
            mark = '● ' if i == ts.auto_start else '  '
            ai = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                mark + lbl, 'menuSetAutoStart:', '')
            ai.setTarget_(self); ai.setRepresentedObject_(str(i))
            asub.addItem_(ai)
        ai_top = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            f'自動開始: {AUTO_START_MODES[ts.auto_start]}', None, '')
        ai_top.setSubmenu_(asub); menu.addItem_(ai_top)

        # #30: always show dots
        mark_d = '✓ ' if ts.always_dots else '  '
        item(mark_d + 'ドットを常時表示', 'menuToggleAlwaysDots:')

        # Login Items
        ll = f'ログイン時に自動起動: {"ON → OFF" if ts.auto_launch else "OFF → ON"}'
        item(ll, 'menuToggleAutoLaunch:')

        menu.addItem_(NSMenuItem.separatorItem())

        # Statistics + CSV
        stat = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            f'今日: {ts.history.today_count()}個 / 今週: {ts.history.week_count()}個', None, '')
        stat.setEnabled_(False); menu.addItem_(stat)
        item('履歴を CSV に出力…', 'menuExportCSV:')

        menu.addItem_(NSMenuItem.separatorItem())

        # #32: keyboard shortcuts list
        sc_hdr = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            'キーボードショートカット:', None, '')
        sc_hdr.setEnabled_(False); menu.addItem_(sc_hdr)
        for line in SHORTCUTS_HELP:
            li = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                f'  {line}', None, '')
            li.setEnabled_(False); menu.addItem_(li)

        menu.addItem_(NSMenuItem.separatorItem())

        # #36: version
        ver = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            f'Pomodoro Timer v{__version__}', None, '')
        ver.setEnabled_(False); menu.addItem_(ver)

        item('終了', 'menuQuit:')
        return menu

    def _refresh(self):
        self.setNeedsDisplay_(True)

    # ── Menu actions ──────────────────────────────────────────────────────────
    def menuReset_(self, _):
        try: self.ts.reset(); self._refresh()
        except Exception as e: _log(f'menuReset_ error: {e}')

    def menuSkip_(self, _):
        try: self.ts.skip(); self._refresh()
        except Exception as e: _log(f'menuSkip_ error: {e}')

    def menuSetMemo_(self, _):   # #34
        try:
            alert = NSAlert.alloc().init()
            alert.setMessageText_('セッションメモを入力')
            alert.setInformativeText_('このセッションのタスク名や目標を記録します')
            alert.addButtonWithTitle_('OK')
            alert.addButtonWithTitle_('キャンセル')
            tf = NSTextField.alloc().initWithFrame_(NSMakeRect(0, 0, 250, 24))
            tf.setStringValue_(self.ts.current_memo or '')
            alert.setAccessoryView_(tf)
            if alert.runModal() == 1000:   # NSAlertFirstButtonReturn
                self.ts.current_memo = str(tf.stringValue())
        except Exception as e:
            _log(f'menuSetMemo_ error: {e}')

    def menuSetWork_(self, sender):
        try:
            v = int(str(sender.representedObject()))
            self.ts.work_duration = v
            if self.ts.is_focus and self.ts.state == TimerState.IDLE:
                self.ts.total_secs = self.ts._paused_rem = self.ts.remaining = float(v)
            self.ts.save(); self._refresh()
        except Exception as e: _log(f'menuSetWork_ error: {e}')

    def menuSetBreak_(self, sender):
        try:
            v = int(str(sender.representedObject()))
            self.ts.break_duration = v
            if not self.ts.is_focus and self.ts.state == TimerState.IDLE:
                self.ts.total_secs = self.ts._paused_rem = self.ts.remaining = float(v)
            self.ts.save(); self._refresh()
        except Exception as e: _log(f'menuSetBreak_ error: {e}')

    def menuSetTheme_(self, sender):
        try:
            key = str(sender.representedObject())
            if key in THEMES:
                self.ts.color_theme = key; self.ts.save(); self._refresh()
        except Exception as e: _log(f'menuSetTheme_ error: {e}')

    def menuSetOpacity_(self, sender):
        try:
            v = max(0.1, min(1.0, float(str(sender.representedObject()))))
            self.ts.opacity = v; self.ts.save()
            self.window().setAlphaValue_(v); self._refresh()
        except Exception as e: _log(f'menuSetOpacity_ error: {e}')

    def menuSetSound_(self, sender):
        try:
            snd = str(sender.representedObject())
            if snd in SOUNDS:
                self.ts.notify_sound = snd; self.ts.save()
                if snd != 'なし':
                    s = NSSound.soundNamed_(snd); s and s.play()
        except Exception as e: _log(f'menuSetSound_ error: {e}')

    def menuSetAutoStart_(self, sender):
        try:
            self.ts.auto_start = int(str(sender.representedObject()))
            self.ts.save(); self._refresh()
        except Exception as e: _log(f'menuSetAutoStart_ error: {e}')

    def menuToggleAlwaysDots_(self, _):   # #30
        try:
            self.ts.always_dots = not self.ts.always_dots
            self.ts.save(); self._refresh()
        except Exception as e: _log(f'menuToggleAlwaysDots_ error: {e}')

    def menuToggleAutoLaunch_(self, _):
        try:
            if not HAS_SM:
                a = NSAlert.alloc().init()
                a.setMessageText_('自動起動には macOS 13 以降が必要です')
                a.runModal(); return
            # #3: detect non-.app execution
            bundle = str(NSApplication.sharedApplication().bundlePath() or '')
            if not bundle.endswith('.app'):
                a = NSAlert.alloc().init()
                a.setMessageText_('スクリプト実行中のため自動起動を設定できません')
                a.setInformativeText_('.app バンドルとして実行してください。')
                a.runModal(); return
            service = SMAppService.mainAppService()
            if self.ts.auto_launch:
                service.unregisterAndReturnError_(None)
                self.ts.auto_launch = False
            else:
                service.registerAndReturnError_(None)
                self.ts.auto_launch = True
            self.ts.save(); self._refresh()
        except Exception as e: _log(f'menuToggleAutoLaunch_ error: {e}')

    def menuExportCSV_(self, _):
        try:
            panel = NSSavePanel.savePanel()
            panel.setNameFieldStringValue_('pomodoro_history.csv')
            if panel.runModal() == 1:
                url = panel.URL()
                if url:
                    self.ts.history.export_csv(url.path())
        except Exception as e: _log(f'menuExportCSV_ error: {e}')

    def menuQuit_(self, _):
        try:
            self.ts.save()
            NSApplication.sharedApplication().terminate_(None)
        except Exception as e: _log(f'menuQuit_ error: {e}')

    # ── Hover ──────────────────────────────────────────────────────────────────
    def mouseEntered_(self, event):
        if self.ts: self.ts.hover = True;  self.setNeedsDisplay_(True)

    def mouseExited_(self, event):
        if self.ts: self.ts.hover = False; self.setNeedsDisplay_(True)

    def updateTrackingAreas(self):
        for a in self.trackingAreas():
            self.removeTrackingArea_(a)
        area = NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(
            self.bounds(),
            NSTrackingMouseEnteredAndExited | NSTrackingActiveAlways,
            self, None)
        self.addTrackingArea_(area)


# ── App Delegate (#25: plain Python attrs instead of objc.ivar) ───────────────
class AppDelegate(NSObject):

    def applicationDidFinishLaunching_(self, _):
        _setup_log()
        ts = TimerState()

        # Window position
        if ts.cfg_x is not None and ts.cfg_y is not None and ts.coords_version >= 2:
            x, y = self._validated_pos(ts.cfg_x, ts.cfg_y)
        else:
            f = NSScreen.mainScreen().visibleFrame()
            x = int(f.origin.x + f.size.width  - W - 20)
            y = int(f.origin.y + f.size.height - W - 20)

        # Transparent floating panel
        panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(x, y, W, W), 0, NSBackingStoreBuffered, False)
        panel.setBackgroundColor_(NSColor.clearColor())
        panel.setOpaque_(False)
        panel.setHasShadow_(False)
        panel.setLevel_(NSFloatingWindowLevel)
        panel.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces |
            NSWindowCollectionBehaviorStationary)
        panel.setIgnoresMouseEvents_(False)
        panel.setMovableByWindowBackground_(False)
        panel.setHidesOnDeactivate_(False)
        panel.setAlphaValue_(ts.opacity)

        view = TimerView.alloc().initWithFrame_(NSMakeRect(0, 0, W, W))
        view.ts = ts
        panel.setContentView_(view)
        view.updateTrackingAreas()
        panel.orderFrontRegardless()

        self._panel       = panel
        self._view        = view
        self._ts          = ts
        self._key_monitor = None   # #12: ivar not global

        # Tick timer (#1: update() called here)
        self._tick_timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.15, self, 'tick:', None, True)

        # Status bar item
        self._setup_status_item()

        # Global keyboard shortcuts (#2: permission check)
        self._setup_key_monitor()

        # Sleep / wake notifications (#35)
        ws_nc = NSWorkspace.sharedWorkspace().notificationCenter()
        ws_nc.addObserver_selector_name_object_(
            self, 'workspaceWillSleep:', 'NSWorkspaceWillSleepNotification', None)
        ws_nc.addObserver_selector_name_object_(
            self, 'workspaceDidWake:', 'NSWorkspaceDidWakeNotification', None)

        # UNUserNotificationCenter auth
        if HAS_UN:
            try:
                UNUserNotificationCenter.currentNotificationCenter() \
                    .requestAuthorizationWithOptions_completionHandler_(
                        UNAuthorizationOptionAlert | UNAuthorizationOptionSound,
                        lambda granted, err: None)
            except Exception:
                pass

    def _validated_pos(self, x, y):
        for scr in NSScreen.screens():
            f = scr.visibleFrame()
            if (f.origin.x <= x <= f.origin.x + f.size.width and
                    f.origin.y <= y <= f.origin.y + f.size.height):
                return x, y
        f = NSScreen.mainScreen().visibleFrame()
        return (int(f.origin.x + f.size.width - W - 20),
                int(f.origin.y + f.size.height - W - 20))

    def _setup_status_item(self):
        try:
            si = NSStatusBar.systemStatusBar().statusItemWithLength_(
                NSVariableStatusItemLength)
            si.button().setTitle_('⏱')
            smenu = NSMenu.alloc().init()
            def si_item(title, sel):
                it = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, sel, '')
                it.setTarget_(self); smenu.addItem_(it)
            si_item('一時停止 / 再開', 'siToggle:')
            si_item('スキップ',        'siSkip:')
            si_item('リセット',        'siReset:')
            smenu.addItem_(NSMenuItem.separatorItem())
            si_item('終了',            'siQuit:')
            si.setMenu_(smenu)
            self._status_item = si
        except Exception as e:
            _log(f'StatusItem error: {e}')
            self._status_item = None

    def _setup_key_monitor(self):
        # #2: warn if key monitor fails (likely Accessibility permission)
        try:
            self._key_monitor = NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
                _KEY_DOWN_MASK, self._on_key)
        except Exception as e:
            _log(f'Global key monitor failed (Accessibility permission required): {e}')
            self._key_monitor = None

    def _on_key(self, event):
        try:
            flags = int(event.modifierFlags())
            ch    = event.charactersIgnoringModifiers()
            if not (flags & CMD_FLAG and flags & SHF_FLAG):
                return
            ts = self._view.ts if self._view else None
            if not ts: return
            if   ch == 'p': ts.handle_click(); self._view.setNeedsDisplay_(True)
            elif ch == 'n': ts.skip();         self._view.setNeedsDisplay_(True)
            elif ch == 'r': ts.reset();        self._view.setNeedsDisplay_(True)
        except Exception as e:
            _log(f'Key handler error: {e}')

    # ── Tick (#1: update() called here, drawRect_ is read-only) ───────────────
    def tick_(self, _):
        if self._view and self._view.ts:
            self._view.ts.update()          # #1: state update separated
        self._view.setNeedsDisplay_(True)
        try:
            if self._status_item and self._view and self._view.ts:
                ts = self._view.ts
                if ts.state in (TimerState.RUNNING, TimerState.PAUSED):
                    m, s  = divmod(int(ts.remaining), 60)
                    icon  = '⏸' if ts.state == TimerState.PAUSED else ('🍅' if ts.is_focus else '☕')
                    self._status_item.button().setTitle_(f'{icon} {m:02d}:{s:02d}')
                else:
                    self._status_item.button().setTitle_('⏱')
        except Exception:
            pass

    # ── Sleep / wake (#35) ────────────────────────────────────────────────────
    def workspaceWillSleep_(self, _):
        try:
            if self._view and self._view.ts:
                self._view.ts.sleep_pause()
        except Exception:
            pass

    def workspaceDidWake_(self, _):
        try:
            ts = self._view.ts if self._view else None
            if ts and ts._sleep_paused:
                ts._sleep_paused = False
                ts._notify('Pomodoro Timer', 'おかえりなさい！タイマーを再開しますか？')
        except Exception:
            pass

    # ── Status item actions ───────────────────────────────────────────────────
    def siToggle_(self, _):
        try:
            if self._view and self._view.ts:
                self._view.ts.handle_click(); self._view.setNeedsDisplay_(True)
        except Exception as e: _log(f'siToggle_ error: {e}')

    def siSkip_(self, _):
        try:
            if self._view and self._view.ts:
                self._view.ts.skip(); self._view.setNeedsDisplay_(True)
        except Exception as e: _log(f'siSkip_ error: {e}')

    def siReset_(self, _):
        try:
            if self._view and self._view.ts:
                self._view.ts.reset(); self._view.setNeedsDisplay_(True)
        except Exception as e: _log(f'siReset_ error: {e}')

    def siQuit_(self, _):
        try:
            if self._view and self._view.ts:
                self._view.ts.save()
            NSApplication.sharedApplication().terminate_(None)
        except Exception as e: _log(f'siQuit_ error: {e}')

    # ── App lifecycle ─────────────────────────────────────────────────────────
    def applicationWillTerminate_(self, _):
        try:   # NSTimer cleanup
            if self._tick_timer:
                self._tick_timer.invalidate()
        except Exception:
            pass
        try:   # #12: key monitor ivar
            if self._key_monitor:
                NSEvent.removeMonitor_(self._key_monitor)
                self._key_monitor = None
        except Exception:
            pass
        try:   # #9: NSWorkspace observer cleanup
            NSWorkspace.sharedWorkspace().notificationCenter().removeObserver_(self)
        except Exception:
            pass
        try:   # #10: status item release
            if self._status_item:
                NSStatusBar.systemStatusBar().removeStatusItem_(self._status_item)
                self._status_item = None
        except Exception:
            pass

    def applicationShouldTerminateAfterLastWindowClosed_(self, _):
        return False


# ── Entry point ───────────────────────────────────────────────────────────────
def main():
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    delegate = AppDelegate.alloc().init()
    app.setDelegate_(delegate)
    app.activateIgnoringOtherApps_(True)
    app.run()


if __name__ == '__main__':
    main()
