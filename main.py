#!/usr/bin/env python3
"""Pomodoro Timer v2 – Pure AppKit transparent floating overlay"""

import time, json, os, tempfile, subprocess, csv, shutil, math
from datetime import date, timedelta
import objc
from AppKit import (
    NSApplication, NSPanel, NSView, NSColor, NSBezierPath,
    NSFont, NSFontAttributeName, NSForegroundColorAttributeName,
    NSAttributedString, NSParagraphStyleAttributeName,
    NSMutableParagraphStyle, NSCenterTextAlignment,
    NSShadow, NSShadowAttributeName,
    NSMenu, NSMenuItem, NSTrackingArea,
    NSAlert, NSStatusBar, NSVariableStatusItemLength,
    NSBackingStoreBuffered, NSFloatingWindowLevel,
    NSMakeRect, NSMakePoint, NSMakeSize,
    NSTrackingMouseEnteredAndExited, NSTrackingActiveAlways,
    NSApplicationActivationPolicyAccessory,
    NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorStationary,
    NSScreen, NSSound, NSWorkspace, NSEvent, NSSavePanel,
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
    _KEY_DOWN_MASK = 1024   # NSKeyDownMask fallback

CMD_FLAG = 1 << 20   # NSCommandKeyMask
SHF_FLAG = 1 << 17   # NSShiftKeyMask

# ── Paths & layout constants ──────────────────────────────────────────────────
_BASE        = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH  = os.path.join(_BASE, 'config.json')
BACKUP_PATH  = CONFIG_PATH + '.backup'
HISTORY_DIR  = os.path.expanduser('~/.pomodoro-timer')
HISTORY_PATH = os.path.join(HISTORY_DIR, 'history.json')

W  = 140
CX = CY = 70.0
R  = 55.0
COORDS_VERSION = 2

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

SOUNDS          = ['Glass', 'Tink', 'Bell', 'Blow', 'Bottle', 'Frog',
                   'Funk', 'Morse', 'Pop', 'Purr', 'Sosumi', 'Submarine', 'なし']
WORK_OPTIONS    = [15*60, 25*60, 30*60, 45*60, 50*60, 60*60, 90*60]
BREAK_OPTIONS   = [5*60, 10*60, 15*60, 20*60]
OPACITY_OPTIONS = [(1.0, '100%'), (0.6, '60%'), (0.3, '30%')]
AUTO_START_MODES = ['手動', 'フェーズ自動', '完全自動']

_key_monitor_ref = None   # global ref to prevent GC


def ns(h: str, a: float = 1.0) -> NSColor:
    r = int(h[1:3], 16) / 255
    g = int(h[3:5], 16) / 255
    b = int(h[5:7], 16) / 255
    return NSColor.colorWithSRGBRed_green_blue_alpha_(r, g, b, a)


def _is_dark() -> bool:
    try:
        name = str(NSApplication.sharedApplication().effectiveAppearance().name())
        return 'Dark' in name
    except Exception:
        return False


def _resolve_theme(key: str) -> dict:
    if key == 'auto':
        return THEMES['mono' if _is_dark() else 'blue']
    return THEMES.get(key, THEMES['blue'])


# ── History ───────────────────────────────────────────────────────────────────
class TimerHistory:
    def __init__(self):
        os.makedirs(HISTORY_DIR, exist_ok=True)
        self._d: dict = {}
        try:
            with open(HISTORY_PATH) as f:
                self._d = json.load(f)
        except FileNotFoundError:
            pass
        except json.JSONDecodeError as e:
            print(f'history.json error: {e}', flush=True)

    def _save(self):
        try:
            fd, tmp = tempfile.mkstemp(dir=HISTORY_DIR)
            with os.fdopen(fd, 'w') as f:
                json.dump(self._d, f)
            os.replace(tmp, HISTORY_PATH)
        except Exception as e:
            print(f'history save error: {e}', flush=True)

    def record(self):
        k = date.today().isoformat()
        e = self._d.setdefault(k, {'count': 0, 'times': []})
        e['count'] += 1
        e['times'].append(int(time.time()))
        self._save()

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
        self.history     = TimerHistory()
        self.state       = self.IDLE
        self.is_focus    = True
        self.total_secs  = float(self.work_duration)
        self.remaining   = float(self.work_duration)
        self._paused_rem = float(self.work_duration)
        self._mono_start = 0.0
        self._flash_t    = 0.0
        self._trans_t    = 0.0   # IDLE→RUNNING arc fade-in start
        self.hover       = False

    # ── Config ────────────────────────────────────────────────────────────────
    def _load_config(self):
        d = dict(work_duration=25*60, break_duration=5*60,
                 window_x=None, window_y=None, coords_version=0,
                 pomodoro_count=0, last_date='',
                 auto_start=0, color_theme='blue', opacity=1.0,
                 notify_sound='Glass', auto_launch=False)
        # backup before load (#29)
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
        except json.JSONDecodeError as e:        # #7: distinguish parse errors
            print(f'config.json decode error: {e} – falling back to backup', flush=True)
            try:
                with open(BACKUP_PATH) as f:
                    d.update(json.load(f))
            except Exception:
                pass

        self.work_duration  = int(d['work_duration'])
        self.break_duration = int(d['break_duration'])
        self.cfg_x = d['window_x']
        self.cfg_y = d['window_y']
        self.coords_version = int(d.get('coords_version', 0))

        today = date.today().isoformat()
        last  = d.get('last_date', '')
        self.pomodoro_count = 0 if last != today else int(d.get('pomodoro_count', 0))
        self.last_date      = today

        raw = d.get('auto_start', 0)
        if isinstance(raw, bool):   # backwards compat
            raw = 2 if raw else 0
        self.auto_start = max(0, min(2, int(raw)))

        self.color_theme = d.get('color_theme', 'blue')
        if self.color_theme not in THEMES:
            self.color_theme = 'blue'

        self.opacity      = max(0.1, min(1.0, float(d.get('opacity', 1.0))))  # #22
        self.notify_sound = d.get('notify_sound', 'Glass')
        if self.notify_sound not in SOUNDS:
            self.notify_sound = 'Glass'
        self.auto_launch = bool(d.get('auto_launch', False))

    def save(self, wx=None, wy=None):
        data = dict(work_duration=self.work_duration, break_duration=self.break_duration,
                    window_x=wx, window_y=wy, coords_version=COORDS_VERSION,
                    pomodoro_count=self.pomodoro_count, last_date=self.last_date,
                    auto_start=self.auto_start, color_theme=self.color_theme,
                    opacity=self.opacity, notify_sound=self.notify_sound,
                    auto_launch=self.auto_launch)
        try:
            fd, tmp = tempfile.mkstemp(dir=_BASE)
            with os.fdopen(fd, 'w') as f:
                json.dump(data, f)
            os.replace(tmp, CONFIG_PATH)
        except Exception as e:
            print(f'config save error: {e}', flush=True)    # #8

    # ── Theme helpers ─────────────────────────────────────────────────────────
    @property
    def theme(self) -> dict:
        return _resolve_theme(self.color_theme)   # #24: auto resolves dark/light

    @property
    def accent_hex(self) -> str:
        t = self.theme
        if self.state == self.PAUSED:  return t['paused']
        return t['focus'] if self.is_focus else t['break_']

    def calc_break(self) -> int:
        if self.pomodoro_count > 0 and self.pomodoro_count % self.SET_SIZE == 0:
            return self.LONG_BREAK
        return self.break_duration

    # ── Notifications ─────────────────────────────────────────────────────────
    def _notify(self):
        if self.notify_sound != 'なし':   # #16
            try:
                snd = NSSound.soundNamed_(self.notify_sound)
                if snd:
                    snd.play()
            except Exception:
                pass
        msg = '休憩時間です！' if self.is_focus else '集中を再開しましょう！'
        if HAS_UN:   # #10: UNUserNotificationCenter
            try:
                content = UNMutableNotificationContent.alloc().init()
                content.setTitle_('Pomodoro Timer')
                content.setBody_(msg)
                req = UNNotificationRequest.requestWithIdentifier_content_trigger_(
                    f'pd_{int(time.time())}', content, None)
                UNUserNotificationCenter.currentNotificationCenter() \
                    .addNotificationRequest_withCompletionHandler_(req, None)
                return
            except Exception as e:
                print(f'UNNotification error: {e}', flush=True)
        try:
            subprocess.Popen(['osascript', '-e',
                f'display notification "{msg}" with title "Pomodoro Timer"'])
        except Exception:
            pass

    # ── Timer logic ───────────────────────────────────────────────────────────
    def update(self):
        if self.state == self.RUNNING:
            elapsed = time.monotonic() - self._mono_start   # #1: monotonic = sleep-safe
            self.remaining = max(0.0, self._paused_rem - elapsed)
            if self.remaining <= 0.001:
                self.remaining = 0.0
                self.state     = self.FINISHED
                self._flash_t  = time.monotonic()
                if self.is_focus:
                    self.pomodoro_count += 1
                    self.history.record()   # #15
                self.save()
                self._notify()
        # Auto-advance: mode 1 (phase only) or 2 (full-auto) after 1.5s flash
        if self.state == self.FINISHED and self.auto_start >= 1:
            if time.monotonic() - self._flash_t >= 1.5:
                self._advance_phase()

    def flash_visible(self) -> bool:
        if self.state == self.FINISHED:
            elapsed = time.monotonic() - self._flash_t
            if elapsed > 3.0:
                return True          # stop flashing, show solid
            return elapsed % 0.5 < 0.3
        if self.state == self.PAUSED:
            return time.monotonic() % 1.2 < 0.8
        return True

    def trans_alpha(self) -> float:
        """Arc fade-in multiplier 0→1 over 0.3s on IDLE→RUNNING."""  # #23
        if self.state == self.RUNNING:
            return min(1.0, (time.monotonic() - self._trans_t) / 0.3)
        return 1.0

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
        if   self.state == self.IDLE:    self._do_start()
        elif self.state == self.RUNNING:
            elapsed          = time.monotonic() - self._mono_start
            self._paused_rem = max(0.0, self._paused_rem - elapsed)
            self.remaining   = self._paused_rem
            self.state       = self.PAUSED
        elif self.state == self.PAUSED:
            self._mono_start = time.monotonic()
            self.state       = self.RUNNING
        elif self.state == self.FINISHED:   # #3: stay FINISHED until click
            self._advance_phase()

    def reset(self):
        self.state       = self.IDLE
        self.is_focus    = True
        self.total_secs  = float(self.work_duration)
        self._paused_rem = float(self.work_duration)
        self.remaining   = float(self.work_duration)

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

    def sleep_pause(self):   # #27
        if self.state == self.RUNNING:
            elapsed          = time.monotonic() - self._mono_start
            self._paused_rem = max(0.0, self._paused_rem - elapsed)
            self.remaining   = self._paused_rem
            self.state       = self.PAUSED


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
        return self

    # ── Drawing ───────────────────────────────────────────────────────────────
    def drawRect_(self, rect):
        NSColor.clearColor().set()
        NSBezierPath.fillRect_(self.bounds())

        ts = self.ts
        if ts is None:
            return
        ts.update()

        vis    = ts.flash_visible()
        t      = ts.theme
        acc    = ts.accent_hex
        center = NSMakePoint(CX, CY)
        ta     = ts.trans_alpha()   # fade-in multiplier

        # ── Base ring (#25: 2px for better contrast) ──────────────────────────
        oval_rect = NSMakeRect(CX - R, CY - R, R * 2, R * 2)
        glow = NSBezierPath.bezierPathWithOvalInRect_(oval_rect)
        glow.setLineWidth_(4.0)
        ns(t['base'], 0.25).set()
        glow.stroke()
        ring = NSBezierPath.bezierPathWithOvalInRect_(oval_rect)
        ring.setLineWidth_(2.0)
        ns(t['base'], 0.9).set()
        ring.stroke()

        # ── Arc ───────────────────────────────────────────────────────────────
        if ts.state == ts.IDLE:
            # faint full arc in idle state
            idle = NSBezierPath.bezierPath()
            idle.appendBezierPathWithArcWithCenter_radius_startAngle_endAngle_clockwise_(
                center, R, 90.0, 90.0 - 359.9, True)
            idle.setLineWidth_(2.0)
            ns(acc, 0.18).set()
            idle.stroke()
        elif vis and ts.total_secs > 0:
            ratio = ts.remaining / ts.total_secs
            if ratio > 0.001:
                total_deg = ratio * 360.0
                N    = 16    # #21: 16 segments (was 32)
                step = total_deg / N
                for i in range(N):
                    alpha = (1.0 - (i / N) * 0.65) * ta   # gradient × fade-in (#23)
                    seg_s = 90.0 - i * step
                    seg_e = seg_s - step
                    seg = NSBezierPath.bezierPath()
                    seg.appendBezierPathWithArcWithCenter_radius_startAngle_endAngle_clockwise_(
                        center, R, seg_s, seg_e, True)
                    seg.setLineWidth_(3.0)
                    ns(acc, alpha).set()
                    seg.stroke()

        # ── Completion pulse ring (#12) ───────────────────────────────────────
        if ts.state == ts.FINISHED and vis:
            pulse = NSBezierPath.bezierPathWithOvalInRect_(oval_rect)
            pulse.setLineWidth_(4.0)
            ns(acc, 0.7).set()
            pulse.stroke()

        # ── Time text ────────────────────────────────────────────────────────
        m, s  = divmod(int(ts.remaining), 60)
        font  = (NSFont.fontWithName_size_('Menlo-Bold', 22.0) or
                 NSFont.boldSystemFontOfSize_(22.0))
        shadow = NSShadow.alloc().init()
        shadow.setShadowColor_(NSColor.colorWithWhite_alpha_(0.0, 0.7))
        shadow.setShadowOffset_(NSMakeSize(0, -1))
        shadow.setShadowBlurRadius_(4.0)
        attrs = {
            NSFontAttributeName:            font,
            NSForegroundColorAttributeName: ns(acc, 0.95 if vis else 0.0),
            NSShadowAttributeName:          shadow,
        }
        ns_str = NSAttributedString.alloc().initWithString_attributes_(f'{m:02d}:{s:02d}', attrs)
        sz = ns_str.size()
        ns_str.drawAtPoint_(NSMakePoint(CX - sz.width / 2, CY - sz.height / 2 + 1))

        # ── Mode / status label ───────────────────────────────────────────────
        sfont = NSFont.fontWithName_size_('Menlo', 9.0) or NSFont.systemFontOfSize_(9.0)
        para  = NSMutableParagraphStyle.alloc().init()
        para.setAlignment_(NSCenterTextAlignment)
        label = None
        if ts.state == ts.RUNNING:     # #4: always show mode when active
            mode  = '集中' if ts.is_focus else '休憩'
            until = ts.SET_SIZE - (ts.pomodoro_count % ts.SET_SIZE)
            label = f'{mode}  →{until}' if ts.is_focus else mode
        elif ts.state == ts.PAUSED:    # #2: ⏸ icon
            label = f'⏸ {"集中" if ts.is_focus else "休憩"}'
        elif ts.state == ts.FINISHED:
            label = '✓ 完了'
        elif ts.state == ts.IDLE and ts.hover:
            mins  = (ts.work_duration if ts.is_focus else ts.break_duration) // 60
            label = f'{"集中" if ts.is_focus else "休憩"} {mins}分'

        if label:
            la = {NSFontAttributeName: sfont,
                  NSForegroundColorAttributeName: ns(t['mode'], 0.85),
                  NSParagraphStyleAttributeName:  para}
            ls  = NSAttributedString.alloc().initWithString_attributes_(label, la)
            lsz = ls.size()
            ls.drawAtPoint_(NSMakePoint(CX - lsz.width / 2, CY - R + 6))

        # ── Pomodoro dots + set number (hover) (#13) ──────────────────────────
        if ts.hover:
            n    = ts.SET_SIZE
            done = ts.pomodoro_count % n
            set_n = ts.pomodoro_count // n + 1
            if set_n > 1:
                sl = {NSFontAttributeName: sfont,
                      NSForegroundColorAttributeName: ns(t['mode'], 0.6),
                      NSParagraphStyleAttributeName:  para}
                ss  = NSAttributedString.alloc().initWithString_attributes_(f'Set {set_n}', sl)
                ssz = ss.size()
                ss.drawAtPoint_(NSMakePoint(CX - ssz.width / 2, CY - R + 30))
            spacing = 11.0
            ox = CX - (n - 1) * spacing / 2
            for i in range(n):
                dr    = 3.0
                dx    = ox + i * spacing
                dy    = CY - R + (20 if set_n <= 1 else 20)
                drect = NSMakeRect(dx - dr, dy - dr, dr * 2, dr * 2)
                dot   = NSBezierPath.bezierPathWithOvalInRect_(drect)
                if i < done:
                    ns(acc, 0.9).set(); dot.fill()
                else:
                    ns(t['base'], 0.6).set()
                    dot.setLineWidth_(0.8); dot.stroke()

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
        self._accum_d += math.sqrt(dx * dx + dy * dy)   # #28: euclidean
        if self._accum_d > 10:                           # #28: threshold 10px
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

    # ── Menu builder (#11: radio-style ● labels) ──────────────────────────────
    def _build_menu(self) -> NSMenu:
        ts   = self.ts
        menu = NSMenu.alloc().init()

        def item(title, sel):
            it = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, sel, '')
            it.setTarget_(self)
            menu.addItem_(it)

        item('リセット', 'menuReset:')
        item('スキップ', 'menuSkip:')
        menu.addItem_(NSMenuItem.separatorItem())

        # Work time submenu (#17: expanded options)
        wsub = NSMenu.alloc().init()
        for v in WORK_OPTIONS:
            mark = '● ' if v == ts.work_duration else '  '
            wi = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                mark + f'{v//60}分', 'menuSetWork:', '')
            wi.setTarget_(self); wi.setRepresentedObject_(str(v))
            wsub.addItem_(wi)
        wi_top = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            f'作業時間: {ts.work_duration//60}分', None, '')
        wi_top.setSubmenu_(wsub); menu.addItem_(wi_top)

        # Break time submenu (#17)
        bsub = NSMenu.alloc().init()
        for v in BREAK_OPTIONS:
            mark = '● ' if v == ts.break_duration else '  '
            bi = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                mark + f'{v//60}分', 'menuSetBreak:', '')
            bi.setTarget_(self); bi.setRepresentedObject_(str(v))
            bsub.addItem_(bi)
        bi_top = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            f'休憩時間: {ts.break_duration//60}分', None, '')
        bi_top.setSubmenu_(bsub); menu.addItem_(bi_top)

        menu.addItem_(NSMenuItem.separatorItem())

        # Color theme submenu (#24: 'auto' included)
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

        # Opacity submenu
        osub = NSMenu.alloc().init()
        for v, lbl in OPACITY_OPTIONS:
            mark = '● ' if abs(v - ts.opacity) < 0.05 else '  '
            oi = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                mark + lbl, 'menuSetOpacity:', '')
            oi.setTarget_(self); oi.setRepresentedObject_(str(v))
            osub.addItem_(oi)
        oi_top = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            '透明度', None, '')
        oi_top.setSubmenu_(osub); menu.addItem_(oi_top)

        # Notification sound submenu (#16)
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

        # Auto-start mode (#26: 3 modes)
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

        # Login Items (#14)
        ll = f'ログイン時に自動起動: {"ON → OFF" if ts.auto_launch else "OFF → ON"}'
        it = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(ll, 'menuToggleAutoLaunch:', '')
        it.setTarget_(self); menu.addItem_(it)

        menu.addItem_(NSMenuItem.separatorItem())

        # Statistics (#15) + CSV export (#30)
        stat = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            f'今日: {ts.history.today_count()}個 / 今週: {ts.history.week_count()}個', None, '')
        stat.setEnabled_(False); menu.addItem_(stat)
        it2 = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            '履歴を CSV に出力…', 'menuExportCSV:', '')
        it2.setTarget_(self); menu.addItem_(it2)

        menu.addItem_(NSMenuItem.separatorItem())
        it3 = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_('終了', 'menuQuit:', '')
        it3.setTarget_(self); menu.addItem_(it3)

        return menu

    def _refresh(self):
        self.setNeedsDisplay_(True)

    # ── Menu actions ──────────────────────────────────────────────────────────
    def menuReset_(self, _):
        try: self.ts.reset(); self._refresh()
        except Exception as e: print(f'menuReset_ error: {e}', flush=True)

    def menuSkip_(self, _):
        try: self.ts.skip(); self._refresh()
        except Exception as e: print(f'menuSkip_ error: {e}', flush=True)

    def menuSetWork_(self, sender):
        try:
            v = int(str(sender.representedObject()))
            self.ts.work_duration = v
            if self.ts.is_focus and self.ts.state == TimerState.IDLE:
                self.ts.total_secs = self.ts._paused_rem = self.ts.remaining = float(v)
            self.ts.save(); self._refresh()
        except Exception as e: print(f'menuSetWork_ error: {e}', flush=True)

    def menuSetBreak_(self, sender):
        try:
            v = int(str(sender.representedObject()))
            self.ts.break_duration = v
            if not self.ts.is_focus and self.ts.state == TimerState.IDLE:
                self.ts.total_secs = self.ts._paused_rem = self.ts.remaining = float(v)
            self.ts.save(); self._refresh()
        except Exception as e: print(f'menuSetBreak_ error: {e}', flush=True)

    def menuSetTheme_(self, sender):
        try:
            key = str(sender.representedObject())
            if key in THEMES:
                self.ts.color_theme = key; self.ts.save(); self._refresh()
        except Exception as e: print(f'menuSetTheme_ error: {e}', flush=True)

    def menuSetOpacity_(self, sender):
        try:
            v = max(0.1, min(1.0, float(str(sender.representedObject()))))
            self.ts.opacity = v; self.ts.save()
            self.window().setAlphaValue_(v); self._refresh()
        except Exception as e: print(f'menuSetOpacity_ error: {e}', flush=True)

    def menuSetSound_(self, sender):
        try:
            snd = str(sender.representedObject())
            if snd in SOUNDS:
                self.ts.notify_sound = snd; self.ts.save()
                if snd != 'なし':
                    s = NSSound.soundNamed_(snd)
                    if s: s.play()
        except Exception as e: print(f'menuSetSound_ error: {e}', flush=True)

    def menuSetAutoStart_(self, sender):
        try:
            self.ts.auto_start = int(str(sender.representedObject()))
            self.ts.save(); self._refresh()
        except Exception as e: print(f'menuSetAutoStart_ error: {e}', flush=True)

    def menuToggleAutoLaunch_(self, _):
        try:
            if not HAS_SM:
                a = NSAlert.alloc().init()
                a.setMessageText_('自動起動には macOS 13 以降が必要です')
                a.runModal(); return
            service = SMAppService.mainAppService()
            if self.ts.auto_launch:
                service.unregisterAndReturnError_(None)
                self.ts.auto_launch = False
            else:
                service.registerAndReturnError_(None)
                self.ts.auto_launch = True
            self.ts.save(); self._refresh()
        except Exception as e: print(f'menuToggleAutoLaunch_ error: {e}', flush=True)

    def menuExportCSV_(self, _):    # #30
        try:
            panel = NSSavePanel.savePanel()
            panel.setNameFieldStringValue_('pomodoro_history.csv')
            if panel.runModal() == 1:
                url = panel.URL()
                if url:
                    self.ts.history.export_csv(url.path())
        except Exception as e: print(f'menuExportCSV_ error: {e}', flush=True)

    def menuQuit_(self, _):
        try:
            self.ts.save()
            NSApplication.sharedApplication().terminate_(None)
        except Exception as e: print(f'menuQuit_ error: {e}', flush=True)

    # ── Hover (#20: instant redraw) ────────────────────────────────────────────
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


# ── App Delegate ──────────────────────────────────────────────────────────────
class AppDelegate(NSObject):
    panel       = objc.ivar()
    view        = objc.ivar()
    tick_timer  = objc.ivar()
    status_item = objc.ivar()

    def applicationDidFinishLaunching_(self, _):
        ts = TimerState()

        # Window position (#9: validate against all screens)
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

        self.panel = panel
        self.view  = view

        # Tick timer (150ms)
        self.tick_timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.15, self, 'tick:', None, True)

        # Status bar item (#5)
        self._setup_status_item()

        # Global keyboard monitor (#6)
        self._setup_key_monitor()

        # Sleep notification (#27)
        NSWorkspace.sharedWorkspace().notificationCenter() \
            .addObserver_selector_name_object_(
                self, 'workspaceWillSleep:', 'NSWorkspaceWillSleepNotification', None)

        # UNUserNotificationCenter auth (#10)
        if HAS_UN:
            try:
                UNUserNotificationCenter.currentNotificationCenter() \
                    .requestAuthorizationWithOptions_completionHandler_(
                        UNAuthorizationOptionAlert | UNAuthorizationOptionSound,
                        lambda granted, err: None)
            except Exception:
                pass

    def _validated_pos(self, x, y):    # #9: multi-display check
        for scr in NSScreen.screens():
            f = scr.visibleFrame()
            if (f.origin.x <= x <= f.origin.x + f.size.width and
                    f.origin.y <= y <= f.origin.y + f.size.height):
                return x, y
        f = NSScreen.mainScreen().visibleFrame()
        return (int(f.origin.x + f.size.width - W - 20),
                int(f.origin.y + f.size.height - W - 20))

    def _setup_status_item(self):   # #5
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
            self.status_item = si
        except Exception as e:
            print(f'StatusItem error: {e}', flush=True)
            self.status_item = None

    def _setup_key_monitor(self):   # #6
        global _key_monitor_ref
        try:
            _key_monitor_ref = NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
                _KEY_DOWN_MASK, self._on_key)
        except Exception as e:
            print(f'Global key monitor: {e}', flush=True)

    def _on_key(self, event):
        try:
            flags = int(event.modifierFlags())
            ch    = event.charactersIgnoringModifiers()
            if not (flags & CMD_FLAG and flags & SHF_FLAG):
                return
            ts = self.view.ts if self.view else None
            if not ts: return
            if   ch == 'p': ts.handle_click(); self.view.setNeedsDisplay_(True)
            elif ch == 'n': ts.skip();         self.view.setNeedsDisplay_(True)
            elif ch == 'r': ts.reset();        self.view.setNeedsDisplay_(True)
        except Exception as e:
            print(f'Key handler: {e}', flush=True)

    # ── Tick ──────────────────────────────────────────────────────────────────
    def tick_(self, _):
        self.view.setNeedsDisplay_(True)
        # Update status item label
        try:
            if self.status_item and self.view and self.view.ts:
                ts = self.view.ts
                if ts.state in (TimerState.RUNNING, TimerState.PAUSED):
                    m, s  = divmod(int(ts.remaining), 60)
                    icon  = '⏸' if ts.state == TimerState.PAUSED else ('🍅' if ts.is_focus else '☕')
                    self.status_item.button().setTitle_(f'{icon} {m:02d}:{s:02d}')
                else:
                    self.status_item.button().setTitle_('⏱')
        except Exception:
            pass

    # ── Sleep handler (#27) ───────────────────────────────────────────────────
    def workspaceWillSleep_(self, _):
        try:
            if self.view and self.view.ts:
                self.view.ts.sleep_pause()
        except Exception:
            pass

    # ── Status item actions ───────────────────────────────────────────────────
    def siToggle_(self, _):
        try:
            if self.view and self.view.ts:
                self.view.ts.handle_click(); self.view.setNeedsDisplay_(True)
        except Exception as e: print(f'siToggle_ error: {e}', flush=True)

    def siSkip_(self, _):
        try:
            if self.view and self.view.ts:
                self.view.ts.skip(); self.view.setNeedsDisplay_(True)
        except Exception as e: print(f'siSkip_ error: {e}', flush=True)

    def siReset_(self, _):
        try:
            if self.view and self.view.ts:
                self.view.ts.reset(); self.view.setNeedsDisplay_(True)
        except Exception as e: print(f'siReset_ error: {e}', flush=True)

    def siQuit_(self, _):
        try:
            if self.view and self.view.ts:
                self.view.ts.save()
            NSApplication.sharedApplication().terminate_(None)
        except Exception as e: print(f'siQuit_ error: {e}', flush=True)

    # ── App lifecycle ─────────────────────────────────────────────────────────
    def applicationWillTerminate_(self, _):
        global _key_monitor_ref
        try:   # #19: cleanup NSTimer
            if self.tick_timer:
                self.tick_timer.invalidate()
        except Exception:
            pass
        try:
            if _key_monitor_ref:
                NSEvent.removeMonitor_(_key_monitor_ref)
                _key_monitor_ref = None
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
