#!/usr/bin/env python3
"""Pomodoro Timer – Pure AppKit transparent floating overlay"""

import time, json, os, tempfile, subprocess
from datetime import date
import objc
from AppKit import (
    NSApplication, NSPanel, NSView, NSColor, NSBezierPath,
    NSFont, NSFontAttributeName, NSForegroundColorAttributeName,
    NSAttributedString, NSParagraphStyleAttributeName,
    NSMutableParagraphStyle, NSCenterTextAlignment,
    NSShadow, NSShadowAttributeName,
    NSMenu, NSMenuItem, NSTrackingArea,
    NSBackingStoreBuffered, NSFloatingWindowLevel,
    NSMakeRect, NSMakePoint, NSMakeSize,
    NSTrackingMouseEnteredAndExited, NSTrackingActiveAlways,
    NSApplicationActivationPolicyAccessory,
    NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorStationary,
    NSScreen, NSSound,
)
from Foundation import NSObject, NSTimer

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.json')
W = 140          # window size (points)
CX, CY, R = 70.0, 70.0, 55.0  # circle center and radius
COORDS_VERSION = 2  # AppKit座標系マーカー（旧tkinter座標との区別用）

# ---------------------------------------------------------------------------
# Color themes
# ---------------------------------------------------------------------------
THEMES = {
    'blue':    dict(focus='#6BA3E0', break_='#6BC4BA', paused='#A090C8',
                    base='#2D3748', mode='#4A6A7A', label='ブルー'),
    'classic': dict(focus='#E07070', break_='#5CC4BC', paused='#E8C84A',
                    base='#3A3A3A', mode='#787878', label='クラシック'),
    'purple':  dict(focus='#9B7FC8', break_='#5CBCB0', paused='#E0A060',
                    base='#3A3050', mode='#786890', label='パープル'),
    'mono':    dict(focus='#B0B8C0', break_='#909898', paused='#C8CCD0',
                    base='#485060', mode='#607080', label='モノクロ'),
}


def ns(h: str, a: float = 1.0) -> NSColor:
    r, g, b = int(h[1:3], 16) / 255, int(h[3:5], 16) / 255, int(h[5:7], 16) / 255
    return NSColor.colorWithSRGBRed_green_blue_alpha_(r, g, b, a)


# ---------------------------------------------------------------------------
# Timer state (pure Python, no UI)
# ---------------------------------------------------------------------------
class TimerState:
    IDLE, RUNNING, PAUSED, FINISHED = 'idle', 'running', 'paused', 'finished'
    WORK_OPTIONS    = [25 * 60, 50 * 60]
    BREAK_OPTIONS   = [5 * 60, 10 * 60]
    OPACITY_OPTIONS = [1.0, 0.6, 0.3]
    LONG_BREAK = 15 * 60
    SET_SIZE   = 4

    def __init__(self):
        self._load_config()
        self.state       = self.IDLE
        self.is_focus    = True
        self.total_secs  = self.work_duration
        self.remaining   = self.work_duration
        self._start_time = None
        self._paused_rem = None
        self._flash_t    = None
        self.hover       = False

    def _load_config(self):
        d = dict(work_duration=25*60, break_duration=5*60,
                 window_x=None, window_y=None, coords_version=0,
                 pomodoro_count=0, last_date='',
                 auto_start=False, color_theme='blue', opacity=1.0)
        try:
            with open(CONFIG_PATH) as f:
                d.update(json.load(f))
        except Exception:
            pass
        self.work_duration  = int(d['work_duration'])
        self.break_duration = int(d['break_duration'])
        self.cfg_x = d['window_x']
        self.cfg_y = d['window_y']
        self.coords_version = int(d.get('coords_version', 0))
        # 日付が変わったらポモドーロカウントをリセット
        today = date.today().isoformat()
        last_date = d.get('last_date', '')
        self.pomodoro_count = 0 if last_date != today else int(d.get('pomodoro_count', 0))
        self.last_date   = today
        self.auto_start  = bool(d.get('auto_start', False))
        self.color_theme = d.get('color_theme', 'blue')
        if self.color_theme not in THEMES:
            self.color_theme = 'blue'
        self.opacity = float(d.get('opacity', 1.0))

    def save(self, wx=None, wy=None):
        data = dict(work_duration=self.work_duration,
                    break_duration=self.break_duration,
                    window_x=wx, window_y=wy,
                    coords_version=COORDS_VERSION,
                    pomodoro_count=self.pomodoro_count,
                    last_date=self.last_date,
                    auto_start=self.auto_start,
                    color_theme=self.color_theme,
                    opacity=self.opacity)
        try:
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(CONFIG_PATH))
            with os.fdopen(fd, 'w') as f:
                json.dump(data, f)
            os.replace(tmp, CONFIG_PATH)
        except Exception:
            pass

    @property
    def theme(self):
        return THEMES[self.color_theme]

    @property
    def accent_hex(self):
        t = self.theme
        if self.state == self.PAUSED: return t['paused']
        return t['focus'] if self.is_focus else t['break_']

    def calc_break(self):
        if self.pomodoro_count > 0 and self.pomodoro_count % self.SET_SIZE == 0:
            return self.LONG_BREAK
        return self.break_duration

    def _notify_finish(self):
        """タイマー終了時に音と通知を送る"""
        # システムサウンド
        try:
            snd = NSSound.soundNamed_('Glass')
            if snd:
                snd.play()
        except Exception:
            pass
        # macOS通知
        try:
            msg = '休憩時間です！' if self.is_focus else '集中を再開しましょう！'
            subprocess.Popen(['osascript', '-e',
                f'display notification "{msg}" with title "Pomodoro Timer"'])
        except Exception:
            pass

    def update(self):
        if self.state == self.RUNNING:
            self.remaining = max(0, int(self._paused_rem - (time.time() - self._start_time)))
            if self.remaining == 0:
                self.state = self.FINISHED
                self._flash_t = time.time()
                if self.is_focus:
                    self.pomodoro_count += 1
                self.save()
                self._notify_finish()
        if self.state == self.FINISHED and time.time() - self._flash_t >= 1.5:
            self.is_focus = not self.is_focus
            dur = self.work_duration if self.is_focus else self.calc_break()
            self.total_secs, self.remaining, self.state = dur, dur, self.IDLE
            if self.auto_start:
                self._do_start()

    def flash_visible(self):
        if self.state == self.FINISHED:
            return (time.time() - self._flash_t) % 0.5 < 0.3
        if self.state == self.PAUSED:
            return time.time() % 1.2 < 0.8
        return True

    def _do_start(self):
        self.state = self.RUNNING
        self._start_time = time.time()
        self._paused_rem = self.remaining

    def handle_click(self):
        if   self.state == self.IDLE:    self._do_start()
        elif self.state == self.RUNNING:
            self._paused_rem = max(0, self._paused_rem - (time.time() - self._start_time))
            self.remaining = int(self._paused_rem)
            self.state = self.PAUSED
        elif self.state == self.PAUSED:
            self._start_time = time.time()
            self.state = self.RUNNING

    def reset(self):
        self.state = self.IDLE
        self.is_focus = True
        self.remaining = self.work_duration
        self.total_secs = self.work_duration

    def skip(self):
        self.is_focus = not self.is_focus
        if not self.is_focus:
            self.pomodoro_count += 1
        dur = self.work_duration if self.is_focus else self.calc_break()
        self.remaining = self.total_secs = dur
        self.state = self.IDLE
        self.save()


# ---------------------------------------------------------------------------
# Custom NSView – draws the timer directly with AppKit APIs
# ---------------------------------------------------------------------------
class TimerView(NSView):

    def isOpaque(self):
        return False  # transparent view

    def initWithFrame_(self, frame):
        self = objc.super(TimerView, self).initWithFrame_(frame)
        if self is not None:
            self.ts = None
            self._press   = None
            self._moved   = False
            self._accum_d = 0.0
        return self

    # ── Drawing ──────────────────────────────────────────────────────────────
    def drawRect_(self, rect):
        # Fully transparent background
        NSColor.clearColor().set()
        NSBezierPath.fillRect_(self.bounds())

        ts = self.ts
        if ts is None:
            return
        ts.update()
        vis = ts.flash_visible()
        t   = ts.theme
        acc = ts.accent_hex
        center = NSMakePoint(CX, CY)

        # ── Base ring (subtle glow + stroke) ─────────────────────────────────
        oval_rect = NSMakeRect(CX - R, CY - R, R * 2, R * 2)
        glow = NSBezierPath.bezierPathWithOvalInRect_(oval_rect)
        glow.setLineWidth_(4.0)
        ns(t['base'], 0.25).set()
        glow.stroke()
        ring = NSBezierPath.bezierPathWithOvalInRect_(oval_rect)
        ring.setLineWidth_(1.0)
        ns(t['base'], 0.9).set()
        ring.stroke()

        # ── Arc ──────────────────────────────────────────────────────────────
        if ts.state == ts.IDLE:
            # アイドル時: フルアークを薄く表示
            idle = NSBezierPath.bezierPath()
            idle.appendBezierPathWithArcWithCenter_radius_startAngle_endAngle_clockwise_(
                center, R, 90.0, 90.0 - 359.9, True)
            idle.setLineWidth_(2.0)
            ns(acc, 0.20).set()
            idle.stroke()
        elif vis and ts.total_secs > 0:
            # グラデーションアーク (32セグメント)
            ratio = ts.remaining / ts.total_secs
            if ratio > 0.001:
                total_deg = ratio * 360.0
                N = 32
                step = total_deg / N
                for i in range(N):
                    alpha = 1.0 - (i / N) * 0.65
                    seg_start = 90.0 - i * step
                    seg_end   = seg_start - step
                    seg = NSBezierPath.bezierPath()
                    seg.appendBezierPathWithArcWithCenter_radius_startAngle_endAngle_clockwise_(
                        center, R, seg_start, seg_end, True)
                    seg.setLineWidth_(3.0)
                    ns(acc, alpha).set()
                    seg.stroke()

        # ── Time text ────────────────────────────────────────────────────────
        m, s = divmod(ts.remaining, 60)
        time_str = f'{m:02d}:{s:02d}'
        font = (NSFont.fontWithName_size_('Menlo-Bold', 22.0) or
                NSFont.fontWithName_size_('Courier Bold', 22.0) or
                NSFont.boldSystemFontOfSize_(22.0))
        shadow = NSShadow.alloc().init()
        shadow.setShadowColor_(NSColor.colorWithWhite_alpha_(0.0, 0.7))
        shadow.setShadowOffset_(NSMakeSize(0, -1))
        shadow.setShadowBlurRadius_(4.0)
        attrs = {
            NSFontAttributeName: font,
            NSForegroundColorAttributeName: ns(acc, 0.95 if vis else 0.0),
            NSShadowAttributeName: shadow,
        }
        ns_str = NSAttributedString.alloc().initWithString_attributes_(time_str, attrs)
        sz = ns_str.size()
        ns_str.drawAtPoint_(NSMakePoint(CX - sz.width / 2, CY - sz.height / 2 + 1))

        # ── Mode / hint label ────────────────────────────────────────────────
        sfont = NSFont.fontWithName_size_('Menlo', 9.0) or \
                NSFont.systemFontOfSize_(9.0)
        para = NSMutableParagraphStyle.alloc().init()
        para.setAlignment_(NSCenterTextAlignment)
        label = None
        if ts.state in (ts.RUNNING, ts.PAUSED):
            label = '集中' if ts.is_focus else '休憩'
            if ts.is_focus:
                until = ts.SET_SIZE - (ts.pomodoro_count % ts.SET_SIZE)
                label += f'  →{until}'
        elif ts.state == ts.IDLE and ts.hover:
            mins = (ts.work_duration if ts.is_focus else ts.break_duration) // 60
            label = f'{"集中" if ts.is_focus else "休憩"} {mins}分'

        if label:
            la = {NSFontAttributeName: sfont,
                  NSForegroundColorAttributeName: ns(t['mode'], 0.85),
                  NSParagraphStyleAttributeName: para}
            ls = NSAttributedString.alloc().initWithString_attributes_(label, la)
            lsz = ls.size()
            ls.drawAtPoint_(NSMakePoint(CX - lsz.width / 2, CY - R + 6))

        # ── Pomodoro dots (hover) ─────────────────────────────────────────────
        if ts.hover:
            n, done = ts.SET_SIZE, ts.pomodoro_count % ts.SET_SIZE
            spacing = 11.0
            ox = CX - (n - 1) * spacing / 2
            for i in range(n):
                dr = 3.0
                dx, dy = ox + i * spacing, CY - R + 18
                drect = NSMakeRect(dx - dr, dy - dr, dr * 2, dr * 2)
                dot = NSBezierPath.bezierPathWithOvalInRect_(drect)
                if i < done:
                    ns(acc, 0.9).set()
                    dot.fill()
                else:
                    ns(t['base'], 0.6).set()
                    dot.setLineWidth_(0.8)
                    dot.stroke()

    # ── Mouse events ─────────────────────────────────────────────────────────
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
        dy = -event.deltaY()   # AppKit Y軸は上向き正なので反転
        self._accum_d += abs(dx) + abs(dy)
        if self._accum_d > 5:
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
        self._press   = None
        self._moved   = False
        self._accum_d = 0.0

    def rightMouseDown_(self, event):
        if not self.ts:
            return
        ts = self.ts
        menu = NSMenu.alloc().init()

        def item(title, sel):
            it = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, sel, '')
            it.setTarget_(self)
            menu.addItem_(it)

        item('リセット',  'menuReset:')
        item('スキップ', 'menuSkip:')
        menu.addItem_(NSMenuItem.separatorItem())

        wi = ts.WORK_OPTIONS.index(ts.work_duration) if ts.work_duration in ts.WORK_OPTIONS else 0
        nw = ts.WORK_OPTIONS[(wi + 1) % len(ts.WORK_OPTIONS)]
        item(f'作業時間: {ts.work_duration//60}分 → {nw//60}分', 'menuToggleWork:')

        bi = ts.BREAK_OPTIONS.index(ts.break_duration) if ts.break_duration in ts.BREAK_OPTIONS else 0
        nb = ts.BREAK_OPTIONS[(bi + 1) % len(ts.BREAK_OPTIONS)]
        item(f'休憩時間: {ts.break_duration//60}分 → {nb//60}分', 'menuToggleBreak:')

        menu.addItem_(NSMenuItem.separatorItem())

        # カラーテーマサブメニュー
        sub = NSMenu.alloc().init()
        for key, th in THEMES.items():
            mark = '● ' if key == ts.color_theme else '  '
            si = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                mark + th['label'], 'menuSetTheme:', '')
            si.setTarget_(self)
            si.setRepresentedObject_(key)
            sub.addItem_(si)
        theme_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            'カラーテーマ', None, '')
        theme_item.setSubmenu_(sub)
        menu.addItem_(theme_item)

        # 透明度サブメニュー
        opacity_sub = NSMenu.alloc().init()
        for v, label in [(1.0, '100%'), (0.6, '60%'), (0.3, '30%')]:
            mark = '● ' if abs(v - ts.opacity) < 0.05 else '  '
            oi = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                mark + label, 'menuSetOpacity:', '')
            oi.setTarget_(self)
            oi.setRepresentedObject_(str(v))
            opacity_sub.addItem_(oi)
        opacity_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            '透明度', None, '')
        opacity_item.setSubmenu_(opacity_sub)
        menu.addItem_(opacity_item)

        menu.addItem_(NSMenuItem.separatorItem())
        auto_lbl = f'自動開始: {"ON → OFF" if ts.auto_start else "OFF → ON"}'
        item(auto_lbl, 'menuToggleAuto:')
        menu.addItem_(NSMenuItem.separatorItem())
        item('終了', 'menuQuit:')

        NSMenu.popUpContextMenu_withEvent_forView_(menu, event, self)

    def _refresh(self):
        self.setNeedsDisplay_(True)

    def menuReset_(self, sender):
        try:
            self.ts.reset()
            self._refresh()
        except Exception as e:
            print(f'menuReset_ error: {e}')

    def menuSkip_(self, sender):
        try:
            self.ts.skip()
            self._refresh()
        except Exception as e:
            print(f'menuSkip_ error: {e}')

    def menuToggleWork_(self, sender):
        try:
            ts = self.ts
            wi = ts.WORK_OPTIONS.index(ts.work_duration) if ts.work_duration in ts.WORK_OPTIONS else 0
            ts.work_duration = ts.WORK_OPTIONS[(wi + 1) % len(ts.WORK_OPTIONS)]
            if ts.is_focus and ts.state == ts.IDLE:
                ts.remaining = ts.total_secs = ts.work_duration
            ts.save()
            self._refresh()
        except Exception as e:
            print(f'menuToggleWork_ error: {e}')

    def menuToggleBreak_(self, sender):
        try:
            ts = self.ts
            bi = ts.BREAK_OPTIONS.index(ts.break_duration) if ts.break_duration in ts.BREAK_OPTIONS else 0
            ts.break_duration = ts.BREAK_OPTIONS[(bi + 1) % len(ts.BREAK_OPTIONS)]
            if not ts.is_focus and ts.state == ts.IDLE:
                ts.remaining = ts.total_secs = ts.break_duration
            ts.save()
            self._refresh()
        except Exception as e:
            print(f'menuToggleBreak_ error: {e}')

    def menuSetTheme_(self, sender):
        try:
            key = str(sender.representedObject())
            if key in THEMES:
                self.ts.color_theme = key
                self.ts.save()
                self._refresh()
        except Exception as e:
            print(f'menuSetTheme_ error: {e}')

    def menuSetOpacity_(self, sender):
        try:
            v = float(str(sender.representedObject()))
            self.ts.opacity = v
            self.ts.save()
            self.window().setAlphaValue_(v)
            self._refresh()
        except Exception as e:
            print(f'menuSetOpacity_ error: {e}')

    def menuToggleAuto_(self, sender):
        try:
            self.ts.auto_start = not self.ts.auto_start
            self.ts.save()
            self._refresh()
        except Exception as e:
            print(f'menuToggleAuto_ error: {e}')

    def menuQuit_(self, sender):
        try:
            self.ts.save()
            NSApplication.sharedApplication().terminate_(None)
        except Exception as e:
            print(f'menuQuit_ error: {e}')

    # ── Hover tracking ────────────────────────────────────────────────────────
    def mouseEntered_(self, event):
        if self.ts:
            self.ts.hover = True
    def mouseExited_(self, event):
        if self.ts:
            self.ts.hover = False

    def updateTrackingAreas(self):
        for a in self.trackingAreas():
            self.removeTrackingArea_(a)
        opts = NSTrackingMouseEnteredAndExited | NSTrackingActiveAlways
        area = NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(
            self.bounds(), opts, self, None)
        self.addTrackingArea_(area)


# ---------------------------------------------------------------------------
# App delegate
# ---------------------------------------------------------------------------
class AppDelegate(NSObject):
    panel = objc.ivar()
    view  = objc.ivar()
    tick_timer = objc.ivar()

    def applicationDidFinishLaunching_(self, note):
        ts = TimerState()

        # 位置（AppKit: y=0 は画面下端）
        screen = NSScreen.mainScreen().visibleFrame()
        if ts.cfg_x is not None and ts.cfg_y is not None and ts.coords_version >= 2:
            # AppKit座標をそのまま使用
            x, y = ts.cfg_x, ts.cfg_y
        else:
            # デフォルト: 画面右上
            x = screen.origin.x + screen.size.width - W - 20
            y = screen.origin.y + screen.size.height - W - 20

        # 透明フローティングパネル生成
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

        view = TimerView.alloc().initWithFrame_(
            NSMakeRect(0, 0, W, W))
        view.ts = ts
        panel.setContentView_(view)
        view.updateTrackingAreas()
        panel.orderFrontRegardless()

        self.panel = panel
        self.view  = view

        # 定期再描画 (150ms)
        self.tick_timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.15, self, 'tick:', None, True)

    def tick_(self, _):
        self.view.setNeedsDisplay_(True)

    def applicationShouldTerminateAfterLastWindowClosed_(self, _):
        # NSPanel はウィンドウとしてカウントされないため False を返す
        # 終了は右クリック→「終了」から行う
        return False


# ---------------------------------------------------------------------------
def main():
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)  # Dockアイコン非表示
    delegate = AppDelegate.alloc().init()
    app.setDelegate_(delegate)
    app.activateIgnoringOtherApps_(True)
    app.run()


if __name__ == '__main__':
    main()
