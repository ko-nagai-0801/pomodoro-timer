#!/usr/bin/env python3
"""Pomodoro Timer v3.2 – Pure AppKit transparent floating overlay"""

__version__ = '3.2.0'

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
    NSCursor, NSRoundLineCapStyle,
)
from Foundation import NSObject, NSTimer, NSDistributedNotificationCenter

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

# ── Drawing constants (#11) ────────────────────────────────────────────────────
class DC:
    W              = 140
    CX = CY        = 70.0
    R              = 55.0
    GLOW_R         = 63.0   # outer glow radius
    TICK           = 0.15   # NSTimer interval
    ARC_N          = 16     # default arc segments
    ARC_N_WARN     = 20     # < 5 min
    ARC_N_URG      = 24     # < 1 min
    PHASE_FADE     = 0.5    # phase color crossfade (s)
    TRANS_FADE     = 0.6    # IDLE→RUNNING arc fade (s)
    WARN_1MIN      = 60     # first warning threshold (s)
    WARN_30S       = 30     # second warning threshold (s)
    EXTEND_FB      = 1.5    # extend feedback overlay duration (s)
    UNDO_GRACE     = 3.0    # reset/skip undo window (s)
    MAX_EXTEND     = 3*60*60  # max extend cap (3 h)
    WAKE_CD        = 3.0    # wake resume countdown (s)
    IDLE_SKIP_MAX  = 10     # tick-skips for adaptive redraw in IDLE

W = DC.W; CX = DC.CX; CY = DC.CY; R = DC.R  # backward compat
COORDS_VERSION = 2

SHORTCUTS_HELP = [
    'Cmd+Shift+P: 一時停止 / 再開',
    'Cmd+Shift+N: スキップ',
    'Cmd+Shift+R: リセット',
    'Cmd+Shift+E: +5分延長',
    'Cmd+Shift+S: 週間統計',
    'スクロール: IDLE時に時間調整',
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

SOUNDS       = ['Glass', 'Tink', 'Bell', 'Blow', 'Bottle', 'Frog',
                'Funk', 'Morse', 'Pop', 'Purr', 'Sosumi', 'Submarine', 'なし']
# #34: added 20min and 120min options
WORK_OPTIONS  = [15*60, 20*60, 25*60, 30*60, 45*60, 50*60, 60*60, 90*60, 120*60]
BREAK_OPTIONS = [5*60, 10*60, 15*60, 20*60]
OPACITY_OPTIONS  = [(1.0, '100%'), (0.6, '60%'), (0.3, '30%')]
AUTO_START_MODES = ['手動', 'フェーズ自動', '完全自動']

# #33: achievement definitions (id, display name, total count threshold)
ACHIEVEMENTS = [
    ('first',   '🎯 初めての集中',    1),
    ('ten',     '🔥 10回達成',       10),
    ('fifty',   '💪 50回達成',       50),
    ('hundred', '🏆 100回達成',     100),
    ('fivehun', '⭐ 500回達成',     500),
]

# ── Paths ─────────────────────────────────────────────────────────────────────
_APP_SUPPORT   = os.path.expanduser('~/Library/Application Support/PomodoroTimer')
CONFIG_PATH    = os.path.join(_APP_SUPPORT, 'config.json')
BACKUP_PATH    = CONFIG_PATH + '.backup'
HISTORY_PATH   = os.path.join(_APP_SUPPORT, 'history.json')
LOG_PATH       = os.path.join(_APP_SUPPORT, 'pomodoro.log')
_LEGACY_BASE   = os.path.dirname(os.path.abspath(__file__))
_LEGACY_CONFIG = os.path.join(_LEGACY_BASE, 'config.json')
_LEGACY_HIST   = os.path.expanduser('~/.pomodoro-timer/history.json')

# ── Logging ───────────────────────────────────────────────────────────────────
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

# ── Color helpers (#1/#26: rgb cache avoids repeated hex parsing) ─────────────
_rgb_cache: dict = {}

def ns(h: str, a: float = 1.0) -> NSColor:
    if h not in _rgb_cache:
        _rgb_cache[h] = (int(h[1:3], 16) / 255,
                         int(h[3:5], 16) / 255,
                         int(h[5:7], 16) / 255)
    r, g, b = _rgb_cache[h]
    return NSColor.colorWithSRGBRed_green_blue_alpha_(r, g, b, a)

def _lerp_hex(a: str, b: str, t: float) -> str:
    if a not in _rgb_cache:
        _rgb_cache[a] = (int(a[1:3],16)/255, int(a[3:5],16)/255, int(a[5:7],16)/255)
    if b not in _rgb_cache:
        _rgb_cache[b] = (int(b[1:3],16)/255, int(b[3:5],16)/255, int(b[5:7],16)/255)
    ar, ag, ab = _rgb_cache[a]; br, bg, bb = _rgb_cache[b]
    return '#{:02X}{:02X}{:02X}'.format(
        int((ar + (br-ar)*t)*255), int((ag + (bg-ag)*t)*255), int((ab + (bb-ab)*t)*255))

# #3/#14: dark mode cache with lock and instant invalidation
_dark_lock    = threading.Lock()
_dark_cache   = False
_dark_cache_t = 0.0

def _is_dark() -> bool:
    global _dark_cache, _dark_cache_t
    now = time.monotonic()
    with _dark_lock:
        if now - _dark_cache_t < 5.0:
            return _dark_cache
        try:
            name = str(NSApplication.sharedApplication().effectiveAppearance().name())
            _dark_cache = 'Dark' in name
        except Exception:
            _dark_cache = False
        _dark_cache_t = now
        return _dark_cache

def _invalidate_dark_cache():
    global _dark_cache_t
    with _dark_lock:
        _dark_cache_t = 0.0

def _resolve_theme(key: str) -> dict:
    return THEMES['mono' if _is_dark() else 'blue'] if key == 'auto' else THEMES.get(key, THEMES['blue'])

def _should_reduce_motion() -> bool:
    """F28: respect macOS Reduce Motion accessibility setting."""
    try:
        return bool(NSWorkspace.sharedWorkspace().accessibilityDisplayShouldReduceMotion())
    except Exception:
        return False

# ── Backup (#23) ──────────────────────────────────────────────────────────────
def _do_backup():
    def _run():
        try:
            backup_dir = os.path.join(_APP_SUPPORT, 'backups', date.today().isoformat())
            os.makedirs(backup_dir, exist_ok=True)
            for src in [CONFIG_PATH, HISTORY_PATH]:
                if os.path.exists(src):
                    dst = os.path.join(backup_dir, os.path.basename(src))
                    if not os.path.exists(dst):
                        shutil.copy2(src, dst)
            # Keep only last 7 daily backups
            backup_base = os.path.join(_APP_SUPPORT, 'backups')
            if os.path.isdir(backup_base):
                days = sorted(d for d in os.listdir(backup_base)
                              if os.path.isdir(os.path.join(backup_base, d)))
                for old in days[:-7]:
                    shutil.rmtree(os.path.join(backup_base, old), ignore_errors=True)
        except Exception as e:
            _log(f'backup error: {e}')
    threading.Thread(target=_run, daemon=True).start()

# ── Helper: representedObject (#12) ───────────────────────────────────────────
def _rep_str(sender) -> str:
    try:
        return str(sender.representedObject() or '')
    except Exception:
        return ''

def _rep_int(sender, default: int = 0) -> int:
    try:
        return int(_rep_str(sender))
    except (ValueError, TypeError):
        return default

def _rep_float(sender, default: float = 0.0) -> float:
    try:
        return float(_rep_str(sender))
    except (ValueError, TypeError):
        return default

# ── History ───────────────────────────────────────────────────────────────────
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
                    raw = json.load(f)
                # G32: validate entries
                cleaned = {}
                for k, v in raw.items():
                    if not isinstance(v, dict):
                        continue
                    cnt = v.get('count', 0)
                    cleaned[k] = {'count': max(0, int(cnt)),
                                  'times': v.get('times', []) if isinstance(v.get('times'), list) else []}
                self._d = cleaned
                if path == _LEGACY_HIST:
                    self._save_bg()
                return
            except FileNotFoundError:
                continue
            except json.JSONDecodeError as e:
                _log(f'history.json parse error: {e}')

    def _trim(self):
        # A3: remove entire entries beyond MAX_AGE_DAYS (not just 'times')
        cutoff = (date.today() - timedelta(days=self._MAX_AGE_DAYS)).isoformat()
        for k in [k for k in self._d if k < cutoff]:
            del self._d[k]

    def _save_sync(self):
        try:
            with self._lock:
                self._trim()
                snap = dict(self._d)
            fd, tmp = tempfile.mkstemp(dir=_APP_SUPPORT)
            with os.fdopen(fd, 'w') as f:
                json.dump(snap, f)
            os.replace(tmp, HISTORY_PATH)
        except (OSError, IOError) as e:
            _log(f'history save error: {e}')

    def _save_bg(self):
        threading.Thread(target=self._save_sync, daemon=True).start()

    def record(self, memo: str = '') -> list:
        """Record a pomodoro; returns list of newly unlocked achievement names."""
        k = date.today().isoformat()
        with self._lock:
            e = self._d.setdefault(k, {'count': 0, 'times': []})
            e['count'] += 1
            e['times'].append({'t': int(time.time()), 'memo': memo} if memo
                              else int(time.time()))
        new_ach = self.check_achievements()
        self._save_bg()
        return new_ach

    def today_count(self) -> int:
        # A1: lock for thread-safe read
        with self._lock:
            return self._d.get(date.today().isoformat(), {}).get('count', 0)

    def week_count(self) -> int:
        today = date.today()
        with self._lock:
            return sum(
                self._d.get((today - timedelta(days=i)).isoformat(), {}).get('count', 0)
                for i in range(7))

    def today_focus_mins(self, work_duration: int) -> int:
        return self.today_count() * work_duration // 60

    def streak(self) -> int:
        """Consecutive days with ≥1 pomodoro ending today."""
        today = date.today()
        n = 0
        with self._lock:
            for i in range(365):
                k = (today - timedelta(days=i)).isoformat()
                if self._d.get(k, {}).get('count', 0) >= 1:
                    n += 1
                else:
                    break
        return n

    def weekly_data(self) -> list:
        """Returns list of (date_str, count) for last 7 days, oldest first."""
        today = date.today()
        return [(( today - timedelta(days=6-i)).isoformat(),
                  self._d.get((today - timedelta(days=6-i)).isoformat(), {}).get('count', 0))
                for i in range(7)]

    def check_achievements(self) -> list:
        """Returns newly unlocked achievement display names."""
        total = sum(e.get('count', 0) for e in self._d.values())
        ach_path = os.path.join(_APP_SUPPORT, 'achievements.json')
        try:
            with open(ach_path) as f:
                already = set(json.load(f))
        except (FileNotFoundError, json.JSONDecodeError):
            already = set()
        new = []
        for aid, name, threshold in ACHIEVEMENTS:
            if aid not in already and total >= threshold:
                new.append(name)
                already.add(aid)
        if new:
            try:
                fd, tmp = tempfile.mkstemp(dir=_APP_SUPPORT)
                with os.fdopen(fd, 'w') as f:
                    json.dump(list(already), f)
                os.replace(tmp, ach_path)
            except (OSError, IOError) as e:
                _log(f'achievements save error: {e}')
        return new

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
        # #8: phase color crossfade
        self._old_accent_hex = ''
        self._phase_change_t = 0.0
        # #9: staged warnings
        self._warned_60  = False
        self._warned_30  = False
        # D21: undo last reset/skip
        self._undo_snap  = None   # dict or None
        self._undo_t     = 0.0
        # C11: extend feedback overlay
        self._extend_t   = 0.0
        # C10: set completion gold pulse
        self._set_complete_t = 0.0
        # D20: wake countdown
        self._wake_cd_t      = 0.0
        self._wake_cd_active = False

    def _load_config(self):
        os.makedirs(_APP_SUPPORT, exist_ok=True)
        d = dict(work_duration=25*60, break_duration=5*60,
                 window_x=None, window_y=None, coords_version=0,
                 pomodoro_count=0, last_date='',
                 auto_start=0, color_theme='blue', opacity=1.0,
                 notify_sound='Glass', auto_launch=False, always_dots=False)
        if not os.path.exists(CONFIG_PATH) and os.path.exists(_LEGACY_CONFIG):
            try:
                shutil.copy2(_LEGACY_CONFIG, CONFIG_PATH)
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
            _log(f'config decode error: {e} – using backup')
            try:
                with open(BACKUP_PATH) as f:
                    d.update(json.load(f))
            except (FileNotFoundError, json.JSONDecodeError):
                pass
        except (OSError, IOError) as e:
            _log(f'config load error: {e}')

        self.work_duration  = max(60, min(120*60, int(d['work_duration'])))
        self.break_duration = max(60, min(30*60,  int(d['break_duration'])))
        self.cfg_x          = d['window_x']
        self.cfg_y          = d['window_y']
        self.coords_version = int(d.get('coords_version', 0))

        today = date.today().isoformat()
        self.pomodoro_count = 0 if d.get('last_date','') != today else int(d.get('pomodoro_count',0))
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
            except (OSError, IOError) as e:
                _log(f'config save error: {e}')
        threading.Thread(target=_do, daemon=True).start()

    @property
    def theme(self) -> dict:
        return _resolve_theme(self.color_theme)

    @property
    def accent_hex(self) -> str:
        t = self.theme
        if self.state == self.PAUSED:
            new_hex = t['paused']
        elif self.is_focus and self.state == self.RUNNING and 0 < self.remaining < 5*60:
            ratio = (1.0 - self.remaining / (5*60)) * 0.6
            new_hex = _lerp_hex(t['focus'], '#E8A060', ratio)
        else:
            new_hex = t['focus'] if self.is_focus else t['break_']
        # #8: phase crossfade
        if self._old_accent_hex:
            elapsed = time.monotonic() - self._phase_change_t
            if elapsed < DC.PHASE_FADE:
                return _lerp_hex(self._old_accent_hex, new_hex, elapsed / DC.PHASE_FADE)
            else:
                self._old_accent_hex = ''
        return new_hex

    def calc_break(self) -> int:
        if self.pomodoro_count > 0 and self.pomodoro_count % self.SET_SIZE == 0:
            return self.LONG_BREAK
        return self.break_duration

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
                content.setTitle_(title); content.setBody_(body)
                req = UNNotificationRequest.requestWithIdentifier_content_trigger_(
                    f'pd_{int(time.time())}', content, None)
                UNUserNotificationCenter.currentNotificationCenter() \
                    .addNotificationRequest_withCompletionHandler_(
                        req,
                        lambda _, err: _log(f'UN error: {err}') if err else None)
                return
            except Exception as e:
                _log(f'UNNotification error: {e}')
        try:
            subprocess.Popen(['osascript', '-e',
                f'display notification "{body}" with title "{title}"'])
        except Exception:
            pass

    def update(self):
        """Called from tick_, not drawRect_."""
        if self.state == self.RUNNING:
            elapsed = time.monotonic() - self._mono_start
            self.remaining = max(0.0, self._paused_rem - elapsed)

            # #9: staged warnings
            if self.remaining <= DC.WARN_30S and not self._warned_30:
                self._warned_30 = True
                try:
                    snd = NSSound.soundNamed_('Tink')
                    if snd: snd.play()
                except Exception:
                    pass
            elif self.remaining <= DC.WARN_1MIN and not self._warned_60:
                self._warned_60 = True
                try:
                    snd = NSSound.soundNamed_('Tink')
                    if snd: snd.play()
                except Exception:
                    pass

            if self.remaining <= 0.001:
                self.remaining = 0.0
                self.state    = self.FINISHED
                self._flash_t = time.monotonic()
                if self.is_focus:
                    self.pomodoro_count += 1
                    new_ach = self.history.record(self.current_memo)
                    self.current_memo = ''
                    for ach in new_ach:
                        self._notify('🏆 実績解除！', ach)
                self.save()
                self._notify()

        if self.state == self.FINISHED and self.auto_start >= 1:
            if time.monotonic() - self._flash_t >= 3.0:
                self._advance_phase()

        # D20: wake countdown auto-resume
        if self._wake_cd_active:
            if time.monotonic() - self._wake_cd_t >= DC.WAKE_CD:
                self._wake_cd_active = False
                if self.state == self.PAUSED:
                    self._mono_start = time.monotonic()
                    self.state = self.RUNNING

    def flash_visible(self) -> bool:
        if self.state == self.FINISHED:
            e = time.monotonic() - self._flash_t
            return True if e > 3.0 else e % 0.5 < 0.3
        return True

    def paused_alpha(self) -> float:
        if self.state == self.PAUSED:
            return 0.35 + 0.65 * (0.5 + 0.5 * math.sin(time.monotonic() * math.pi * 1.2))
        return 1.0

    def trans_alpha(self) -> float:
        if self.state == self.RUNNING:
            p = min(1.0, (time.monotonic() - self._trans_t) / DC.TRANS_FADE)
            return p * p * (3.0 - 2.0 * p)
        return 1.0

    def auto_cd_remaining(self) -> float:
        if self.state == self.FINISHED and self.auto_start >= 1:
            return max(0.0, 3.0 - (time.monotonic() - self._flash_t))
        return 0.0

    @property
    def is_long_break(self) -> bool:
        return not self.is_focus and self.total_secs >= self.LONG_BREAK

    def _do_start(self):
        # G33: single time.monotonic() call
        t = time.monotonic()
        self._trans_t    = t
        self._mono_start = t
        self._warned_60  = False
        self._warned_30  = False
        self.state       = self.RUNNING

    def _advance_phase(self):
        # #8: record old accent for crossfade
        self._old_accent_hex = self.accent_hex
        self._phase_change_t = time.monotonic()
        # C10: detect set completion before toggling
        if self.is_focus and self.pomodoro_count > 0 and self.pomodoro_count % self.SET_SIZE == 0:
            self._set_complete_t = time.monotonic()
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
            self.save()   # A2: save on pause so remaining time survives quit
        elif self.state == self.PAUSED:
            self._mono_start = time.monotonic()
            self.state       = self.RUNNING
        elif self.state == self.FINISHED:
            self._advance_phase()

    def _snap(self) -> dict:
        """D21: capture current state for undo."""
        return dict(state=self.state, is_focus=self.is_focus,
                    total_secs=self.total_secs, remaining=self.remaining,
                    _paused_rem=self._paused_rem, pomodoro_count=self.pomodoro_count,
                    current_memo=self.current_memo)

    def _restore(self, snap: dict):
        for k, v in snap.items():
            setattr(self, k, v)
        self._warned_60 = False; self._warned_30 = False

    def reset(self):
        self._undo_snap = self._snap(); self._undo_t = time.monotonic()  # D21
        self.state        = self.IDLE
        self.is_focus     = True
        self.total_secs   = float(self.work_duration)
        self._paused_rem  = float(self.work_duration)
        self.remaining    = float(self.work_duration)
        self.current_memo = ''
        self._warned_60   = False
        self._warned_30   = False

    def skip(self):
        self._undo_snap = self._snap(); self._undo_t = time.monotonic()  # D21
        # #8: record old accent
        self._old_accent_hex = self.accent_hex
        self._phase_change_t = time.monotonic()
        if self.is_focus:
            self.pomodoro_count += 1
        self.is_focus    = not self.is_focus
        dur              = float(self.work_duration if self.is_focus else self.calc_break())
        self.total_secs  = dur; self._paused_rem = dur; self.remaining = dur
        self.state       = self.IDLE
        self._warned_60  = False; self._warned_30 = False
        self.save()

    def can_undo(self) -> bool:
        return (self._undo_snap is not None and
                time.monotonic() - self._undo_t < DC.UNDO_GRACE)

    def undo(self):
        if self.can_undo():
            self._restore(self._undo_snap)
            self._undo_snap = None

    def extend(self, extra_secs: int = 5*60):
        """#22: extend current session."""
        if self.state in (self.RUNNING, self.PAUSED):
            new_total = self.total_secs + extra_secs
            if new_total > DC.MAX_EXTEND:  # A4: cap
                extra_secs = max(0, int(DC.MAX_EXTEND - self.total_secs))
            if extra_secs > 0:
                self._paused_rem += extra_secs
                self.total_secs  += extra_secs
                self.remaining   += extra_secs
                self._extend_t    = time.monotonic()  # C11: trigger overlay

    def sleep_pause(self):
        if self.state == self.RUNNING:
            elapsed          = time.monotonic() - self._mono_start
            self._paused_rem = max(0.0, self._paused_rem - elapsed)
            self.remaining   = self._paused_rem
            self.state       = self.PAUSED
            self._sleep_paused = True

    def wake_resume_start(self):
        """D20: start on-screen countdown before auto-resume."""
        if self._sleep_paused:
            self._sleep_paused = False
            self._wake_cd_t      = time.monotonic()
            self._wake_cd_active = True

    def wake_cd_remaining(self) -> float:
        if self._wake_cd_active:
            return max(0.0, DC.WAKE_CD - (time.monotonic() - self._wake_cd_t))
        return 0.0

    def reset_pomodoro_count(self):
        """E26: reset today's count."""
        self.pomodoro_count = 0
        self.save()


# ── Stats View (#10: weekly bar chart) ────────────────────────────────────────
class StatsView(NSView):
    def isOpaque(self): return False

    def initWithFrame_(self, frame):
        self = objc.super(StatsView, self).initWithFrame_(frame)
        if self is not None:
            self.data      = []    # [(date_str, count), ...]
            self.theme_key = 'blue'
        return self

    def drawRect_(self, rect):
        NSColor.clearColor().set()
        NSBezierPath.fillRect_(self.bounds())
        if not self.data:
            return

        t = _resolve_theme(self.theme_key)
        acc = t['focus']
        w, h = 320.0, 140.0
        ml, mr, mt, mb = 24.0, 8.0, 16.0, 24.0
        cw = w - ml - mr
        ch = h - mt - mb
        n  = len(self.data)
        max_c = max((c for _, c in self.data), default=1) or 1
        bw = cw / n * 0.65
        gw = cw / n
        font = NSFont.fontWithName_size_('Menlo', 7.5) or NSFont.systemFontOfSize_(7.5)
        para = NSMutableParagraphStyle.alloc().init()
        para.setAlignment_(NSCenterTextAlignment)

        for i, (day_str, count) in enumerate(self.data):
            bx = ml + i * gw + (gw - bw) / 2
            bh = (count / max_c) * ch
            is_today = (i == n - 1)
            bar_rect = NSMakeRect(bx, mb, bw, max(2.0, bh))
            bar = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(bar_rect, 2, 2)
            ns(acc, 0.85 if is_today else 0.45).set()
            bar.fill()
            # date label
            lbl = day_str[5:]  # MM-DD
            la = {NSFontAttributeName: font,
                  NSForegroundColorAttributeName: ns(t['mode'], 0.7 if is_today else 0.5),
                  NSParagraphStyleAttributeName: para}
            ls = NSAttributedString.alloc().initWithString_attributes_(lbl, la)
            lsz = ls.size()
            ls.drawAtPoint_(NSMakePoint(bx + bw/2 - lsz.width/2, 4))
            # count label
            if count > 0:
                cl = NSAttributedString.alloc().initWithString_attributes_(str(count), la)
                clz = cl.size()
                cl.drawAtPoint_(NSMakePoint(bx + bw/2 - clz.width/2, mb + bh + 2))


# ── Timer View ────────────────────────────────────────────────────────────────
class TimerView(NSView):

    def isOpaque(self): return False

    def initWithFrame_(self, frame):
        self = objc.super(TimerView, self).initWithFrame_(frame)
        if self is not None:
            self.ts         = None
            self._press     = False
            self._moved     = False
            self._accum_d   = 0.0
            self._lw_cur    = 3.0   # #18: hover line width smooth transition
            self._font_cache = {}   # #7: font size → NSFont
            self._shadow    = None
            self._para      = None
            self._base_oval = None  # #1: cached base ring rect
            self._glow_oval = None  # B5: cached glow ring rect
            self._time_str_cache: tuple = ('', None)  # B6: (str, NSAttributedString)
        return self

    def _init_resources(self):
        if self._shadow is not None:
            return
        shadow = NSShadow.alloc().init()
        shadow.setShadowColor_(NSColor.colorWithWhite_alpha_(0.0, 0.7))
        shadow.setShadowOffset_(NSMakeSize(0, -1))
        shadow.setShadowBlurRadius_(4.0)
        self._shadow = shadow
        para = NSMutableParagraphStyle.alloc().init()
        para.setAlignment_(NSCenterTextAlignment)
        self._para   = para
        self._base_oval = NSMakeRect(CX - R, CY - R, R * 2, R * 2)          # #1
        gr = DC.GLOW_R
        self._glow_oval = NSMakeRect(CX - gr, CY - gr, gr * 2, gr * 2)      # B5

    def _mk_font(self, size: float) -> NSFont:
        if size not in self._font_cache:
            self._font_cache[size] = (
                NSFont.fontWithName_size_('Menlo-Bold', size) or
                NSFont.boldSystemFontOfSize_(size))
        return self._font_cache[size]

    def _small_font(self) -> NSFont:
        if 9.0 not in self._font_cache:
            self._font_cache[9.0] = (
                NSFont.fontWithName_size_('Menlo', 9.0) or
                NSFont.systemFontOfSize_(9.0))
        return self._font_cache[9.0]

    # ── Drawing ───────────────────────────────────────────────────────────────
    def drawRect_(self, rect):
        NSColor.clearColor().set()
        NSBezierPath.fillRect_(self.bounds())
        ts = self.ts
        if ts is None:
            return
        self._init_resources()

        # #31: single time capture for animation sync
        now    = time.monotonic()
        t      = ts.theme
        acc    = ts.accent_hex
        center = NSMakePoint(CX, CY)
        ta     = ts.trans_alpha()
        pa     = ts.paused_alpha()
        oval   = self._base_oval   # #1: reuse cached rect
        base_a = 0.9 if _is_dark() else 1.0

        # #18: smooth hover line width transition
        lw_target  = 3.6 if ts.hover else 3.0
        self._lw_cur += (lw_target - self._lw_cur) * 0.2
        base_lw = self._lw_cur

        # #16: outer glow ring (B5: use cached oval)
        glow_circ = NSBezierPath.bezierPathWithOvalInRect_(self._glow_oval)
        glow_circ.setLineWidth_(5.0)
        reduce_motion = _should_reduce_motion()
        glow_a = (0.06 + 0.04 * math.sin(now * math.pi * 2.0)
                  if ts.state == ts.RUNNING and not reduce_motion else 0.05)
        ns(acc, glow_a).set()
        glow_circ.stroke()

        # ── Base ring ────────────────────────────────────────────────────────
        glow2 = NSBezierPath.bezierPathWithOvalInRect_(oval)
        glow2.setLineWidth_(4.0)
        ns(t['base'], 0.25).set()
        glow2.stroke()

        # #17: last 30s urgent pulse on base ring
        if ts.state == ts.RUNNING and 0 < ts.remaining < 30 and not reduce_motion:
            pulse_a = 0.2 + 0.4 * abs(math.sin(now * math.pi * 3.3))
            ring = NSBezierPath.bezierPathWithOvalInRect_(oval)
            ring.setLineWidth_(2.5)
            ns(acc, pulse_a).set()
            ring.stroke()
        else:
            ring = NSBezierPath.bezierPathWithOvalInRect_(oval)
            ring.setLineWidth_(2.0)
            ns(t['base'], base_a).set()
            ring.stroke()

        # C9: clock-face tick marks at 12/3/6/9 positions
        for deg in (90.0, 0.0, 270.0, 180.0):
            rad = math.radians(deg)
            ix = CX + (R - 2) * math.cos(rad); iy = CY + (R - 2) * math.sin(rad)
            ox2 = CX + (R + 4) * math.cos(rad); oy2 = CY + (R + 4) * math.sin(rad)
            tick = NSBezierPath.bezierPath()
            tick.moveToPoint_(NSMakePoint(ix, iy))
            tick.lineToPoint_(NSMakePoint(ox2, oy2))
            tick.setLineWidth_(1.5)
            ns(t['base'], 0.6).set()
            tick.stroke()

        # ── Progress arc ──────────────────────────────────────────────────────
        if ts.state == ts.IDLE:
            # #6: breathing idle arc (segmented); C15: center dot
            if not reduce_motion:
                N = 8
                step = 360.0 / N
                for i in range(N):
                    seg_s = 90.0 - i * step
                    seg_e = seg_s - step
                    a = 0.12 + 0.08 * math.sin(now * math.pi * 0.5 + i * math.pi / N)
                    seg = NSBezierPath.bezierPath()
                    seg.appendBezierPathWithArcWithCenter_radius_startAngle_endAngle_clockwise_(
                        center, R, seg_s, seg_e, True)
                    seg.setLineWidth_(2.0)
                    ns(acc, a).set()
                    seg.stroke()
            # C15: small center dot as start indicator
            dot_r = 3.0
            cdot = NSBezierPath.bezierPathWithOvalInRect_(
                NSMakeRect(CX - dot_r, CY - dot_r, dot_r * 2, dot_r * 2))
            ns(acc, 0.25).set(); cdot.fill()

        elif ts.state == ts.PAUSED:
            # #29: dashed arc for PAUSED
            ratio = ts.remaining / max(0.001, ts.total_secs)  # #5: guard
            if ratio > 0.001:
                arc_deg = ratio * 360.0
                arc_path = NSBezierPath.bezierPath()
                arc_path.appendBezierPathWithArcWithCenter_radius_startAngle_endAngle_clockwise_(
                    center, R, 90.0, 90.0 - arc_deg, True)
                arc_path.setLineDash_count_phase_([8.0, 5.0], 2, 0.0)
                arc_path.setLineWidth_(base_lw)
                arc_path.setLineCapStyle_(NSRoundLineCapStyle)
                ns(acc, pa * 0.9).set()
                arc_path.stroke()

        elif ts.total_secs > 0:
            ratio = ts.remaining / max(0.001, ts.total_secs)  # #5: guard
            vis   = ts.flash_visible()
            if ratio > 0.001 and vis:
                total_deg = ratio * 360.0
                # #19: dynamic segment count
                if ts.remaining < 60:
                    N = DC.ARC_N_URG
                elif ts.remaining < 5*60:
                    N = DC.ARC_N_WARN
                else:
                    N = DC.ARC_N
                step = total_deg / N
                # #28: breathing line width
                lw = base_lw + 0.25 * math.sin(now * math.pi * 2.0)
                for i in range(N):
                    seg_s = 90.0 - i * step
                    seg_e = seg_s - step - (1.0 if i < N - 1 else 0)  # overlap
                    alpha = (1.0 - (i / N) * 0.65) * ta
                    seg = NSBezierPath.bezierPath()
                    seg.appendBezierPathWithArcWithCenter_radius_startAngle_endAngle_clockwise_(
                        center, R, seg_s, seg_e, True)
                    seg.setLineWidth_(lw)
                    ns(acc, alpha).set()
                    seg.stroke()

        # C12: seconds arc inside base ring for last 60s
        if ts.state == ts.RUNNING and 0 < ts.remaining <= 60:
            sec_r = R - 8
            sec_ratio = ts.remaining / 60.0
            sec_arc = NSBezierPath.bezierPath()
            sec_arc.appendBezierPathWithArcWithCenter_radius_startAngle_endAngle_clockwise_(
                center, sec_r, 90.0, 90.0 - sec_ratio * 360.0, True)
            sec_arc.setLineWidth_(1.5)
            sec_arc.setLineCapStyle_(NSRoundLineCapStyle)
            ns(acc, 0.45).set()
            sec_arc.stroke()

        # ── Completion pulse ring (C16: expanding ring on FINISHED) ───────────
        if ts.state == ts.FINISHED and ts.flash_visible():
            pulse = NSBezierPath.bezierPathWithOvalInRect_(oval)
            pulse.setLineWidth_(4.0)
            ns(acc, 0.7).set()
            pulse.stroke()
            # C16: expanding ring
            if not reduce_motion:
                fin_elapsed = time.monotonic() - ts._flash_t
                exp_phase = (fin_elapsed % 0.5) / 0.5  # 0..1 per 500ms
                exp_r = R + 5 + exp_phase * 12
                exp_a = 0.5 * (1.0 - exp_phase)
                exp_rect = NSMakeRect(CX - exp_r, CY - exp_r, exp_r * 2, exp_r * 2)
                exp_ring = NSBezierPath.bezierPathWithOvalInRect_(exp_rect)
                exp_ring.setLineWidth_(2.0)
                ns(acc, exp_a).set()
                exp_ring.stroke()

        # C10: set completion gold pulse
        if ts._set_complete_t > 0:
            sc_elapsed = time.monotonic() - ts._set_complete_t
            if sc_elapsed < 2.0 and not reduce_motion:
                sc_phase = (sc_elapsed % 0.4) / 0.4
                sc_a = 0.8 * (1.0 - sc_elapsed / 2.0) * abs(math.sin(sc_phase * math.pi))
                gold_ring = NSBezierPath.bezierPathWithOvalInRect_(oval)
                gold_ring.setLineWidth_(4.0)
                ns('#FFD700', sc_a).set()
                gold_ring.stroke()

        # ── Time text (#7: responsive font size) ──────────────────────────────
        m, s = divmod(int(ts.remaining), 60)
        if m == 0 and s < 10:
            fsize = 28.0
        elif m >= 60:
            fsize = 18.0
        else:
            fsize = 22.0
        t_alpha = 0.95
        if ts.state == ts.PAUSED:
            t_alpha *= pa
        elif ts.state == ts.FINISHED and not ts.flash_visible():
            t_alpha = 0.0
        attrs = {
            NSFontAttributeName:            self._mk_font(fsize),
            NSForegroundColorAttributeName: ns(acc, t_alpha),
            NSShadowAttributeName:          self._shadow,
        }
        time_key = f'{m:02d}:{s:02d}|{acc}|{t_alpha:.2f}|{fsize}'
        if self._time_str_cache[0] == time_key:  # B6: reuse if unchanged
            ns_str = self._time_str_cache[1]
        else:
            ns_str = NSAttributedString.alloc().initWithString_attributes_(f'{m:02d}:{s:02d}', attrs)
            self._time_str_cache = (time_key, ns_str)
        sz = ns_str.size()
        ns_str.drawAtPoint_(NSMakePoint(CX - sz.width / 2, CY - sz.height / 2 + 1))

        # ── Mode / status label (#15: always shown in IDLE) ───────────────────
        label       = None
        label_alpha = 0.85
        # C14: mode emoji; C13: long break distinction
        if ts.is_focus:
            mode_str = '🎯 集中'
        elif ts.is_long_break:
            mode_str = '😴 長い休憩'
        else:
            mode_str = '☕ 休憩'
        if ts.state == ts.RUNNING:
            done  = ts.pomodoro_count % ts.SET_SIZE
            total = ts.SET_SIZE
            # E22: show memo in hover
            if ts.hover and ts.current_memo:
                memo_short = ts.current_memo[:18] + ('…' if len(ts.current_memo) > 18 else '')
                label = memo_short
            elif ts.hover:
                label = f'{mode_str} — 一時停止'
            else:
                label = f'{mode_str} ({done}/{total})'
        elif ts.state == ts.PAUSED:
            label       = f'⏸ {mode_str}'
            label_alpha = pa * 0.85
        elif ts.state == ts.FINISHED:
            label = '✓ 完了 — クリックで次へ'
        elif ts.state == ts.IDLE:
            # #15: always show mode in IDLE; E25: always_dots shows label always
            mins  = (ts.work_duration if ts.is_focus else ts.break_duration) // 60
            label = (f'{mode_str} {mins}分 — クリックで開始' if ts.hover
                     else f'{mode_str} {mins}分')

        if label:
            la = {NSFontAttributeName: self._small_font(),
                  NSForegroundColorAttributeName: ns(t['mode'], label_alpha),
                  NSParagraphStyleAttributeName:  self._para}
            ls  = NSAttributedString.alloc().initWithString_attributes_(label, la)
            lsz = ls.size()
            ls.drawAtPoint_(NSMakePoint(CX - lsz.width / 2, CY - R + 6))

        # #20: auto-start countdown
        cd = ts.auto_cd_remaining()
        if cd > 0:
            cd_text = f'自動開始まで {math.ceil(cd)}秒'
            cd_attr = {NSFontAttributeName: self._small_font(),
                       NSForegroundColorAttributeName: ns(t['mode'], 0.7),
                       NSParagraphStyleAttributeName:  self._para}
            cd_str = NSAttributedString.alloc().initWithString_attributes_(cd_text, cd_attr)
            csz = cd_str.size()
            cd_str.drawAtPoint_(NSMakePoint(CX - csz.width / 2, CY - R + 20))

        # ── Pomodoro dots ──────────────────────────────────────────────────────
        if ts.hover or ts.always_dots:
            n     = ts.SET_SIZE
            done  = ts.pomodoro_count % n
            set_n = ts.pomodoro_count // n + 1
            base_y = CY - R + 22
            if set_n > 1:
                sl = {NSFontAttributeName: self._small_font(),
                      NSForegroundColorAttributeName: ns(t['mode'], 0.6),
                      NSParagraphStyleAttributeName:  self._para}
                ss  = NSAttributedString.alloc().initWithString_attributes_(f'Set {set_n}', sl)
                ssz = ss.size()
                ss.drawAtPoint_(NSMakePoint(CX - ssz.width / 2, base_y + 10))
            spacing = 12.0; ox = CX - (n - 1) * spacing / 2
            for i in range(n):
                dr = 3.5; dx = ox + i * spacing
                drect = NSMakeRect(dx - dr, base_y - dr, dr * 2, dr * 2)
                dot   = NSBezierPath.bezierPathWithOvalInRect_(drect)
                if i < done:
                    ns(acc, 0.9).set(); dot.fill()
                else:
                    ns(t['base'], 0.5).set(); dot.setLineWidth_(1.0); dot.stroke()

        # ── Overlays (extend feedback, wake countdown, undo hint) ─────────────
        def _overlay_text(txt, y_off, color, alpha):
            oa = {NSFontAttributeName: self._small_font(),
                  NSForegroundColorAttributeName: ns(color, alpha),
                  NSParagraphStyleAttributeName:  self._para}
            os = NSAttributedString.alloc().initWithString_attributes_(txt, oa)
            oz = os.size()
            os.drawAtPoint_(NSMakePoint(CX - oz.width / 2, CY + y_off))

        # C11: extend feedback "+5:00"
        if ts._extend_t > 0:
            ext_e = now - ts._extend_t
            if ext_e < DC.EXTEND_FB:
                ext_a = 1.0 - ext_e / DC.EXTEND_FB
                _overlay_text('+5:00', R - 28, acc, ext_a)

        # D20: wake countdown
        wcd = ts.wake_cd_remaining()
        if wcd > 0:
            _overlay_text(f'再開まで {math.ceil(wcd)}秒…', 0, acc, 0.85)

        # D21: undo hint
        if ts.can_undo():
            ud_a = max(0.0, 1.0 - (now - ts._undo_t) / DC.UNDO_GRACE)
            _overlay_text('← 元に戻す (クリック)', -R + 12, t['mode'], ud_a * 0.8)

    # ── Mouse events ──────────────────────────────────────────────────────────
    def acceptsFirstMouse_(self, event): return True

    def mouseDown_(self, event):
        self._press = True; self._moved = False; self._accum_d = 0.0

    def mouseDragged_(self, event):
        if not self._press: return
        dx =  event.deltaX(); dy = -event.deltaY()
        self._accum_d += math.sqrt(dx*dx + dy*dy)
        if self._accum_d > 10: self._moved = True
        if self._moved:
            f = self.window().frame()
            f.origin.x += dx; f.origin.y += dy
            self.window().setFrame_display_(f, True)

    def mouseUp_(self, event):
        if not self._moved and self.ts:
            # D21: undo takes priority if within grace window
            if self.ts.can_undo():
                self.ts.undo()
            else:
                self.ts.handle_click()
            self.setNeedsDisplay_(True)
        elif self._moved and self.ts:
            f  = self.window().frame()
            sx, sy = self._clamp_to_screen(int(f.origin.x), int(f.origin.y))  # #20
            if sx != int(f.origin.x) or sy != int(f.origin.y):
                f.origin.x = sx; f.origin.y = sy
                self.window().setFrame_display_(f, True)
            self.ts.save(sx, sy)
        self._press = False; self._moved = False; self._accum_d = 0.0

    def _clamp_to_screen(self, x: int, y: int):
        """#20: keep window within screen bounds."""
        margin = 10
        for scr in NSScreen.screens():
            f = scr.frame()
            if (f.origin.x - W <= x <= f.origin.x + f.size.width + W and
                    f.origin.y - W <= y <= f.origin.y + f.size.height + W):
                nx = max(f.origin.x + margin, min(f.origin.x + f.size.width - W - margin, x))
                ny = max(f.origin.y + margin, min(f.origin.y + f.size.height - W - margin, y))
                return int(nx), int(ny)
        f = NSScreen.mainScreen().visibleFrame()
        return (int(f.origin.x + f.size.width - W - 20),
                int(f.origin.y + f.size.height - W - 20))

    def rightMouseDown_(self, event):
        if self.ts:
            NSMenu.popUpContextMenu_withEvent_forView_(self._build_menu(), event, self)

    def resetCursorRects(self):
        self.addCursorRect_cursor_(self.bounds(), NSCursor.pointingHandCursor())

    # ── Menu builder (#27: _mk_submenu helper) ────────────────────────────────
    def _mk_submenu(self, items):
        """items: [(title, sel, repr_str, is_selected), ...]"""
        sub = NSMenu.alloc().init()
        for title, sel, repr_str, selected in items:
            mi = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                ('● ' if selected else '  ') + title, sel, '')
            mi.setTarget_(self)
            if repr_str:
                mi.setRepresentedObject_(repr_str)
            sub.addItem_(mi)
        return sub

    def _build_menu(self) -> NSMenu:
        ts   = self.ts
        menu = NSMenu.alloc().init()

        def add(title, sel, enabled=True):
            it = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, sel, '')
            it.setTarget_(self); it.setEnabled_(enabled); menu.addItem_(it)

        add('リセット', 'menuReset:')
        add('スキップ', 'menuSkip:')
        # D21: undo
        undo_title = ('↩ 元に戻す' if ts.can_undo() else '↩ 元に戻す（なし）')
        add(undo_title, 'menuUndo:', ts.can_undo())
        # #22: extend (only when running or paused)
        add('+5分延長', 'menuExtend:',
            ts.state in (ts.RUNNING, ts.PAUSED))
        add('セッションメモを入力…', 'menuSetMemo:')
        menu.addItem_(NSMenuItem.separatorItem())

        note = ' (次回から)' if ts.state == ts.RUNNING else ''

        # Work time submenu (#27)
        wsub = self._mk_submenu([
            (f'{v//60}分{note}', 'menuSetWork:', str(v), v == ts.work_duration)
            for v in WORK_OPTIONS])
        wi = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            f'作業時間: {ts.work_duration//60}分', None, '')
        wi.setSubmenu_(wsub); menu.addItem_(wi)

        # Break time submenu
        bsub = self._mk_submenu([
            (f'{v//60}分{note}', 'menuSetBreak:', str(v), v == ts.break_duration)
            for v in BREAK_OPTIONS])
        bi = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            f'休憩時間: {ts.break_duration//60}分', None, '')
        bi.setSubmenu_(bsub); menu.addItem_(bi)

        menu.addItem_(NSMenuItem.separatorItem())

        # Color theme submenu
        csub = self._mk_submenu([
            (th['label'], 'menuSetTheme:', key, key == ts.color_theme)
            for key, th in THEMES.items()])
        ci = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_('カラーテーマ', None, '')
        ci.setSubmenu_(csub); menu.addItem_(ci)

        # Opacity submenu
        osub = self._mk_submenu([
            (lbl, 'menuSetOpacity:', str(v), abs(v - ts.opacity) < 0.05)
            for v, lbl in OPACITY_OPTIONS])
        oi = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_('透明度', None, '')
        oi.setSubmenu_(osub); menu.addItem_(oi)

        # Sound submenu
        nsub = self._mk_submenu([
            (snd, 'menuSetSound:', snd, snd == ts.notify_sound)
            for snd in SOUNDS])
        ni = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            f'通知音: {ts.notify_sound}', None, '')
        ni.setSubmenu_(nsub); menu.addItem_(ni)

        menu.addItem_(NSMenuItem.separatorItem())

        # Auto-start submenu
        asub = self._mk_submenu([
            (lbl, 'menuSetAutoStart:', str(i), i == ts.auto_start)
            for i, lbl in enumerate(AUTO_START_MODES)])
        ai = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            f'自動開始: {AUTO_START_MODES[ts.auto_start]}', None, '')
        ai.setSubmenu_(asub); menu.addItem_(ai)

        add(('✓ ' if ts.always_dots else '  ') + 'ドットを常時表示', 'menuToggleAlwaysDots:')
        add(f'ログイン時に自動起動: {"ON → OFF" if ts.auto_launch else "OFF → ON"}',
            'menuToggleAutoLaunch:')

        menu.addItem_(NSMenuItem.separatorItem())

        # #21/#25: statistics line
        fmins = ts.history.today_focus_mins(ts.work_duration)
        stk   = ts.history.streak()
        stat  = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            f'今日: {ts.history.today_count()}個  今週: {ts.history.week_count()}個  '
            f'集中: {fmins}分  🔥{stk}日', None, '')
        stat.setEnabled_(False); menu.addItem_(stat)

        add('📊 週間統計を見る', 'menuShowStats:')
        add('📋 履歴を見る', 'menuShowHistory:')
        add('💾 CSV に出力…', 'menuExportCSV:')
        add('🔄 今日のカウントをリセット', 'menuResetCount:')

        menu.addItem_(NSMenuItem.separatorItem())

        for line in SHORTCUTS_HELP:
            li = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                f'  {line}', None, '')
            li.setEnabled_(False); menu.addItem_(li)

        menu.addItem_(NSMenuItem.separatorItem())
        ver = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            f'Pomodoro Timer v{__version__}', None, '')
        ver.setEnabled_(False); menu.addItem_(ver)
        add('終了', 'menuQuit:')
        return menu

    def _refresh(self):
        self.setNeedsDisplay_(True)

    # ── Menu actions ──────────────────────────────────────────────────────────
    def menuReset_(self, _):
        try: self.ts.reset(); self._refresh()
        except Exception as e: _log(f'menuReset_: {e}')

    def menuSkip_(self, _):
        try: self.ts.skip(); self._refresh()
        except Exception as e: _log(f'menuSkip_: {e}')

    def menuUndo_(self, _):    # D21
        try: self.ts.undo(); self._refresh()
        except Exception as e: _log(f'menuUndo_: {e}')

    def menuExtend_(self, _):   # #22
        try: self.ts.extend(); self._refresh()
        except Exception as e: _log(f'menuExtend_: {e}')

    def menuSetMemo_(self, _):
        try:
            alert = NSAlert.alloc().init()
            alert.setMessageText_('セッションメモを入力')
            alert.setInformativeText_('このセッションのタスク名や目標を記録します')
            alert.addButtonWithTitle_('OK')
            alert.addButtonWithTitle_('キャンセル')
            tf = NSTextField.alloc().initWithFrame_(NSMakeRect(0, 0, 250, 24))
            tf.setStringValue_(self.ts.current_memo or '')
            alert.setAccessoryView_(tf)
            if alert.runModal() == 1000:
                self.ts.current_memo = str(tf.stringValue())
        except Exception as e: _log(f'menuSetMemo_: {e}')

    def menuSetWork_(self, sender):
        try:
            v = _rep_int(sender)
            self.ts.work_duration = v
            if self.ts.is_focus and self.ts.state == TimerState.IDLE:
                self.ts.total_secs = self.ts._paused_rem = self.ts.remaining = float(v)
            self.ts.save(); self._refresh()
        except Exception as e: _log(f'menuSetWork_: {e}')

    def menuSetBreak_(self, sender):
        try:
            v = _rep_int(sender)
            self.ts.break_duration = v
            if not self.ts.is_focus and self.ts.state == TimerState.IDLE:
                self.ts.total_secs = self.ts._paused_rem = self.ts.remaining = float(v)
            self.ts.save(); self._refresh()
        except Exception as e: _log(f'menuSetBreak_: {e}')

    def menuSetTheme_(self, sender):
        try:
            key = _rep_str(sender)
            if key in THEMES:
                self.ts.color_theme = key; self.ts.save(); self._refresh()
        except Exception as e: _log(f'menuSetTheme_: {e}')

    def menuSetOpacity_(self, sender):
        try:
            v = max(0.1, min(1.0, _rep_float(sender, 1.0)))
            self.ts.opacity = v; self.ts.save()
            self.window().setAlphaValue_(v); self._refresh()
        except Exception as e: _log(f'menuSetOpacity_: {e}')

    def menuSetSound_(self, sender):
        try:
            snd = _rep_str(sender)
            if snd in SOUNDS:
                self.ts.notify_sound = snd; self.ts.save()
                if snd != 'なし':
                    s = NSSound.soundNamed_(snd); s and s.play()
        except Exception as e: _log(f'menuSetSound_: {e}')

    def menuSetAutoStart_(self, sender):
        try:
            self.ts.auto_start = max(0, min(2, _rep_int(sender)))
            self.ts.save(); self._refresh()
        except Exception as e: _log(f'menuSetAutoStart_: {e}')

    def menuToggleAlwaysDots_(self, _):
        try:
            self.ts.always_dots = not self.ts.always_dots
            self.ts.save(); self._refresh()
        except Exception as e: _log(f'menuToggleAlwaysDots_: {e}')

    def menuToggleAutoLaunch_(self, _):
        try:
            if not HAS_SM:
                a = NSAlert.alloc().init()
                a.setMessageText_('自動起動には macOS 13 以降が必要です')
                a.runModal(); return
            bundle = str(NSApplication.sharedApplication().bundlePath() or '')
            if not bundle.endswith('.app'):
                a = NSAlert.alloc().init()
                a.setMessageText_('スクリプト実行中のため自動起動を設定できません')
                a.setInformativeText_('.app バンドルとして実行してください。')
                a.runModal(); return
            svc = SMAppService.mainAppService()
            if self.ts.auto_launch:
                svc.unregisterAndReturnError_(None); self.ts.auto_launch = False
            else:
                svc.registerAndReturnError_(None);   self.ts.auto_launch = True
            self.ts.save(); self._refresh()
        except Exception as e: _log(f'menuToggleAutoLaunch_: {e}')

    def menuShowStats_(self, _):   # #10
        try:
            data   = self.ts.history.weekly_data()
            sv     = StatsView.alloc().initWithFrame_(NSMakeRect(0, 0, 320, 140))
            sv.data = data; sv.theme_key = self.ts.color_theme
            today_c = self.ts.history.today_count()
            week_c  = self.ts.history.week_count()
            fmins   = self.ts.history.today_focus_mins(self.ts.work_duration)
            stk     = self.ts.history.streak()
            alert = NSAlert.alloc().init()
            alert.setMessageText_('週間ポモドーロ統計')
            alert.setInformativeText_(
                f'今日: {today_c}個  /  今週: {week_c}個  /  本日集中: {fmins}分  /  🔥 連続: {stk}日')
            alert.setAccessoryView_(sv)
            alert.runModal()
        except Exception as e: _log(f'menuShowStats_: {e}')

    def menuShowHistory_(self, _):   # #32 + E24: include memos
        try:
            lines = []
            today = date.today()
            with self.ts.history._lock:
                snap = dict(self.ts.history._d)
            for i in range(30):
                d = (today - timedelta(days=i))
                k = d.isoformat()
                entry = snap.get(k, {})
                count = entry.get('count', 0)
                if count > 0:
                    bar = '●' * min(count, 12) + ('…' if count > 12 else '')
                    # E24: collect unique memos
                    memos = []
                    for t_entry in entry.get('times', []):
                        if isinstance(t_entry, dict) and t_entry.get('memo'):
                            m = t_entry['memo'][:20]
                            if m not in memos:
                                memos.append(m)
                    memo_str = f'  [{", ".join(memos[:3])}]' if memos else ''
                    lines.append(f'{k}  {count:3d}個  {bar}{memo_str}')
            alert = NSAlert.alloc().init()
            alert.setMessageText_('ポモドーロ履歴（直近30日）')
            alert.setInformativeText_('\n'.join(lines) if lines else '履歴がありません')
            alert.runModal()
        except Exception as e: _log(f'menuShowHistory_: {e}')

    def menuExportCSV_(self, _):
        try:
            panel = NSSavePanel.savePanel()
            panel.setNameFieldStringValue_('pomodoro_history.csv')
            if panel.runModal() == 1:
                url = panel.URL()
                # #28: guard against None path
                path = url.path() if url else None
                if path:
                    self.ts.history.export_csv(path)
        except Exception as e: _log(f'menuExportCSV_: {e}')

    def menuResetCount_(self, _):   # E26
        try:
            self.ts.reset_pomodoro_count(); self._refresh()
        except Exception as e: _log(f'menuResetCount_: {e}')

    def menuQuit_(self, _):
        try:
            self.ts.save()
            NSApplication.sharedApplication().terminate_(None)
        except Exception as e: _log(f'menuQuit_: {e}')

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

    # D17: scroll wheel adjusts work/break duration in IDLE
    def scrollWheel_(self, event):
        if not self.ts or self.ts.state != 'idle':
            return
        dy = event.scrollingDeltaY()
        if abs(dy) < 1:
            return
        delta = 5 * 60 if dy > 0 else -5 * 60
        if self.ts.is_focus:
            opts = WORK_OPTIONS
            cur  = self.ts.work_duration
            idx  = min(range(len(opts)), key=lambda i: abs(opts[i] - cur))
            new_idx = max(0, min(len(opts) - 1, idx + (1 if delta > 0 else -1)))
            self.ts.work_duration = opts[new_idx]
            self.ts.total_secs = self.ts._paused_rem = self.ts.remaining = float(opts[new_idx])
        else:
            opts = BREAK_OPTIONS
            cur  = self.ts.break_duration
            idx  = min(range(len(opts)), key=lambda i: abs(opts[i] - cur))
            new_idx = max(0, min(len(opts) - 1, idx + (1 if delta > 0 else -1)))
            self.ts.break_duration = opts[new_idx]
            self.ts.total_secs = self.ts._paused_rem = self.ts.remaining = float(opts[new_idx])
        self.ts.save()
        self.setNeedsDisplay_(True)

    # F27: VoiceOver support (no trailing underscore = zero-arg ObjC getter)
    def accessibilityRole(self):
        return 'AXGroup'

    def accessibilityLabel(self):
        if not self.ts:
            return 'ポモドーロタイマー'
        ts = self.ts
        mode = '集中' if ts.is_focus else '休憩'
        return f'ポモドーロタイマー {mode}モード'

    def accessibilityValue(self):
        if not self.ts:
            return ''
        ts = self.ts
        m, s = divmod(int(ts.remaining), 60)
        state_map = {'idle': '待機中', 'running': '実行中',
                     'paused': '一時停止', 'finished': '完了'}
        st = state_map.get(ts.state, ts.state)
        return f'{st} 残り {m:02d}分{s:02d}秒'

    def isAccessibilityElement(self):
        return True


# ── App Delegate ──────────────────────────────────────────────────────────────
class AppDelegate(NSObject):

    def applicationDidFinishLaunching_(self, _):
        _setup_log()
        _do_backup()   # #23: daily backup on launch

        ts = TimerState()

        if ts.cfg_x is not None and ts.cfg_y is not None and ts.coords_version >= 2:
            x, y = self._validated_pos(ts.cfg_x, ts.cfg_y)
        else:
            f = NSScreen.mainScreen().visibleFrame()
            x = int(f.origin.x + f.size.width  - W - 20)
            y = int(f.origin.y + f.size.height - W - 20)

        panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(x, y, W, W), 0, NSBackingStoreBuffered, False)
        panel.setBackgroundColor_(NSColor.clearColor())
        panel.setOpaque_(False); panel.setHasShadow_(False)
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
        self._key_monitor = None
        self._idle_skip   = 0    # B7: adaptive redraw counter

        self._tick_timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            DC.TICK, self, 'tick:', None, True)

        self._setup_status_item()
        self._setup_key_monitor()

        # Sleep / wake
        ws_nc = NSWorkspace.sharedWorkspace().notificationCenter()
        ws_nc.addObserver_selector_name_object_(
            self, 'workspaceWillSleep:', 'NSWorkspaceWillSleepNotification', None)
        ws_nc.addObserver_selector_name_object_(
            self, 'workspaceDidWake:', 'NSWorkspaceDidWakeNotification', None)

        # #14: instant dark mode response via distributed notification
        try:
            NSDistributedNotificationCenter.defaultCenter() \
                .addObserver_selector_name_object_(
                    self, 'appearanceChanged:',
                    'AppleInterfaceThemeChangedNotification', None)
        except Exception as e:
            _log(f'Appearance notification: {e}')

        if HAS_UN:
            try:
                UNUserNotificationCenter.currentNotificationCenter() \
                    .requestAuthorizationWithOptions_completionHandler_(
                        UNAuthorizationOptionAlert | UNAuthorizationOptionSound,
                        lambda granted, err: None)
            except Exception:
                pass

    def appearanceChanged_(self, _):   # #14
        _invalidate_dark_cache()
        if self._view:
            self._view.setNeedsDisplay_(True)

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
        try:
            self._key_monitor = NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
                _KEY_DOWN_MASK, self._on_key)
        except Exception as e:
            _log(f'Global key monitor failed (check Accessibility permission): {e}')

    def _on_key(self, event):
        try:
            flags = int(event.modifierFlags())
            ch    = event.charactersIgnoringModifiers()
            if not (flags & CMD_FLAG and flags & SHF_FLAG): return
            ts = self._view.ts if self._view else None
            if not ts: return
            if   ch == 'p': ts.handle_click(); self._view.setNeedsDisplay_(True)
            elif ch == 'n': ts.skip();         self._view.setNeedsDisplay_(True)
            elif ch == 'r': ts.reset();        self._view.setNeedsDisplay_(True)
            elif ch == 'e': ts.extend();       self._view.setNeedsDisplay_(True)   # D18
            elif ch == 's':                                                          # D19
                self._view.menuShowStats_(None)
        except Exception as e:
            _log(f'Key handler: {e}')

    # ── Tick (#1: update here, not in drawRect_) ──────────────────────────────
    def tick_(self, _):
        if self._view and self._view.ts:
            ts = self._view.ts
            ts.update()
            # B8: smooth hover line width transition here (not in drawRect_)
            lw_target = 3.6 if ts.hover else 3.0
            self._view._lw_cur += (lw_target - self._view._lw_cur) * 0.2

        # B7: adaptive redraw — skip most ticks when IDLE+no hover
        ts = self._view.ts if self._view else None
        do_draw = True
        if ts and ts.state == 'idle' and not ts.hover and not ts.can_undo():
            self._idle_skip += 1
            if self._idle_skip < DC.IDLE_SKIP_MAX:
                do_draw = False
            else:
                self._idle_skip = 0
        else:
            self._idle_skip = 0

        if do_draw:
            self._view.setNeedsDisplay_(True)

        # #24/E23/F30: richer status bar display
        try:
            if self._status_item and ts:
                if ts.state in (TimerState.RUNNING, TimerState.PAUSED):
                    m, s  = divmod(int(ts.remaining), 60)
                    icon  = '⏸' if ts.state == TimerState.PAUSED else ('🍅' if ts.is_focus else '☕')
                    done  = ts.pomodoro_count % ts.SET_SIZE
                    set_n = ts.pomodoro_count // ts.SET_SIZE + 1
                    self._status_item.button().setTitle_(
                        f'{icon} {m:02d}:{s:02d} ({done}/{ts.SET_SIZE} S{set_n})')
                else:
                    # E23: IDLE shows today's count
                    today_n = ts.history.today_count()
                    lbl = f'⏱ 今日{today_n}個' if today_n > 0 else '⏱'
                    self._status_item.button().setTitle_(lbl)
        except Exception:
            pass

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
                # D20: start 3-second on-screen countdown, then auto-resume
                ts.wake_resume_start()
                ts._notify('Pomodoro Timer', 'おかえりなさい！3秒後に自動再開します…')
        except Exception:
            pass

    def siToggle_(self, _):
        try:
            if self._view and self._view.ts:
                self._view.ts.handle_click(); self._view.setNeedsDisplay_(True)
        except Exception as e: _log(f'siToggle_: {e}')

    def siSkip_(self, _):
        try:
            if self._view and self._view.ts:
                self._view.ts.skip(); self._view.setNeedsDisplay_(True)
        except Exception as e: _log(f'siSkip_: {e}')

    def siReset_(self, _):
        try:
            if self._view and self._view.ts:
                self._view.ts.reset(); self._view.setNeedsDisplay_(True)
        except Exception as e: _log(f'siReset_: {e}')

    def siQuit_(self, _):
        try:
            if self._view and self._view.ts:
                self._view.ts.save()
            NSApplication.sharedApplication().terminate_(None)
        except Exception as e: _log(f'siQuit_: {e}')

    def applicationWillTerminate_(self, _):
        try:
            if self._tick_timer:
                self._tick_timer.invalidate()
                self._tick_timer = None   # #4: clear reference
        except Exception:
            pass
        try:
            if self._key_monitor:
                NSEvent.removeMonitor_(self._key_monitor)
                self._key_monitor = None
        except Exception:
            pass
        try:
            NSWorkspace.sharedWorkspace().notificationCenter().removeObserver_(self)
        except Exception:
            pass
        try:
            NSDistributedNotificationCenter.defaultCenter().removeObserver_(self)
        except Exception:
            pass
        try:
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
