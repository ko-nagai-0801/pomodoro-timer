# macOS ウィンドウ管理・透明化 技術仕様書

## 検証環境

| 項目 | 値 |
|------|-----|
| Platform | macOS (Darwin 25.3.0) |
| Python | 3.12.12 |
| Tcl/Tk | 9.0.3 |
| Screen | 1470x956 |

---

## 1. 透明背景の実現

### 重要: `-transparentcolor` は macOS では使えない

```
bad attribute "-transparentcolor": must be -alpha, -appearance, -buttons,
-fullscreen, -isdark, -modified, -notify, -titlepath, -topmost, -transparent,
-stylemask, -class, -tabbingid, -tabbingmode, or -type
```

`-transparentcolor` は **Windows専用** の属性。macOS (Tk 9.0) では存在しない。

### macOS での正しい方法: `-transparent` + `systemTransparent`

```python
root.wm_attributes('-transparent', True)
root.configure(bg='systemTransparent')

canvas = tk.Canvas(root, width=300, height=300,
                   bg='systemTransparent',
                   highlightthickness=0)
canvas.pack()
```

**仕組み:**
- `-transparent` は NSWindow の `opaque` プロパティを `NO` に、`backgroundColor` を `[NSColor clearColor]` に設定する
- `'systemTransparent'` は Tk 9.0 で使えるシステムカラー名。ウィジェットの背景を透明にする
- Canvas上に描画されたもの（arc, text, line等）だけが表示される
- Canvas自体の背景も透明なので、描画していない部分はデスクトップが透けて見える

### '#000001' パターンが不要な理由

Windows では `#000001`（ほぼ黒）を透明色に指定するテクニックがあるが:
- `#000000`（純黒）を透明色にすると、テキストや影などの黒も透明になってしまう
- `#000001` は見た目はほぼ黒だが、透明色とは区別される

**macOS ではこのテクニック自体が不要。** `-transparent` + `systemTransparent` で完全に解決する。

### クロスプラットフォーム対応（参考）

```python
import sys

if sys.platform == 'darwin':
    root.wm_attributes('-transparent', True)
    bg_color = 'systemTransparent'
elif sys.platform == 'win32':
    bg_color = '#000001'
    root.wm_attributes('-transparentcolor', bg_color)
else:  # Linux
    # X11は透明ウィンドウに対応していない場合が多い
    bg_color = '#1a1a2e'

root.configure(bg=bg_color)
```

今回はmacOS専用なので、`-transparent` + `systemTransparent` のみ使用する。

---

## 2. フレームレスウィンドウ

### `overrideredirect(True)`

```python
root.overrideredirect(True)
```

**効果:**
- タイトルバー、閉じる/最小化/最大化ボタンを非表示
- ウィンドウマネージャの制御から外れる
- 完全にカスタム外観のウィンドウになる

**macOS での挙動:**
- Mission Control に表示されない
- Cmd+Tab のアプリ切替に表示されない（Dockアイコンが無い場合）
- ウィンドウの移動・リサイズは一切できない（自前実装が必要）

### ウィンドウ移動の自前実装

```python
class DraggableWindow:
    def __init__(self, root, canvas):
        self._drag_x = 0
        self._drag_y = 0
        canvas.bind('<Button-1>', self._start_drag)
        canvas.bind('<B1-Motion>', self._on_drag)
        self.root = root

    def _start_drag(self, event):
        self._drag_x = event.x
        self._drag_y = event.y

    def _on_drag(self, event):
        x = self.root.winfo_x() + (event.x - self._drag_x)
        y = self.root.winfo_y() + (event.y - self._drag_y)
        self.root.geometry(f'+{x}+{y}')
```

**注意点:**
- `<Button-1>` はCanvas全体をドラッグ対象にする。特定の領域だけをドラッグ対象にしたい場合はヒットテストを追加する
- `event.x_root` / `event.y_root` ではなく、`winfo_x()` + イベント座標の差分を使う方が安定する

### 代替案: `-stylemask` で部分的にフレーム制御

Tk 9.0 macOS では `-stylemask` で細かく制御可能:

```python
# タイトルバーはあるが、ボタンなし・リサイズ不可
root.wm_attributes('-stylemask', ('titled',))

# 完全にフレームなし（overrideredirectと似た効果）
root.wm_attributes('-stylemask', ())
```

利用可能な stylemask ビット:
- `titled` - タイトルバーあり
- `closable` - 閉じるボタンあり
- `miniaturizable` - 最小化ボタンあり
- `resizable` - リサイズ可能
- `fullsizecontentview` - コンテンツがタイトルバー下まで拡張

**推奨: `overrideredirect(True)` を使用。** 最もシンプルで確実。

---

## 3. 常に最前面

### `wm_attributes('-topmost', True)`

```python
root.wm_attributes('-topmost', True)
```

**挙動:**
- 通常のウィンドウより常に前面に表示
- 他の `-topmost` ウィンドウとは z-order で競合する可能性がある

### フルスクリーンアプリとの関係

| シナリオ | 挙動 |
|---------|------|
| 通常のウィンドウ | 常に前面に表示される |
| フルスクリーンアプリ（macOS native） | **表示されない**。macOSのフルスクリーンは独自のSpaceを作るため |
| 最大化されたウィンドウ | 前面に表示される |
| 他の `-topmost` ウィンドウ | 作成順や最後にフォーカスした順序で重なる |

### Mission Control での挙動

- `overrideredirect(True)` のウィンドウは Mission Control に表示されない
- これはポモドーロタイマーにとって望ましい挙動

### スクリーンセーバー時

- スクリーンセーバーは通常、最前面レベル以上で表示されるため、タイマーは隠れる
- スクリーンセーバー解除後は再び表示される
- **タイマーの計時には影響しない**（time.time() ベースの場合）

---

## 4. Dockアイコン非表示

### 調査結果

| 方法 | 結果 | 評価 |
|------|------|------|
| `-type` attribute | Tk 9.0 で利用可能 | 要検証 |
| `overrideredirect(True)` | Dockに表示されなくなる | **推奨** |
| Info.plist `LSUIElement=1` | .app バンドル時のみ | pyinstaller向け |
| pyobjc | 確実だが依存追加 | 不採用 |

### 推奨: `overrideredirect(True)` で自動的に解決

`overrideredirect(True)` を設定すると、macOS ではウィンドウマネージャの管理外になるため、**Dockアイコンは自動的に表示されなくなる**。

追加の対策は不要。

### 将来 .app バンドルにする場合

PyInstaller等で .app にパッケージングする際は、`Info.plist` に以下を追加:

```xml
<key>LSUIElement</key>
<true/>
```

これにより:
- Dockアイコン非表示
- メニューバーにアプリ名が表示されない
- Cmd+Tab に表示されない

### `-type` による制御（補足）

```python
# Tk 9.0 macOS で利用可能な -type 値（全て動作確認済み）:
# normal, utility, dialog, toolbar, splash, dock,
# desktop, notification, tooltip, panel
```

`-type` は Dock 表示に直接影響しないが、ウィンドウの振る舞い（フォーカス等）に影響する可能性がある。`overrideredirect(True)` と組み合わせる場合は効果が限定的。

---

## 5. スリープ復帰後のタイマードリフト対策

### 問題

```python
# 危険なパターン: after() の間隔を信頼する
self.remaining -= 1
root.after(1000, self.tick)
```

`root.after(1000, ...)` は「1000ms後にコールバックを実行する」という意味だが:
- macOS がスリープすると、after() のタイマーは一時停止する
- スリープから復帰すると、残り時間にスリープ分の誤差が生じる
- GUIイベントの処理負荷でも微小なドリフトが発生する

### 解決策: `time.time()` ベースの経過時間計算

```python
import time

class PomodoroTimer:
    def __init__(self, root, duration_minutes=25):
        self.root = root
        self.duration = duration_minutes * 60  # 秒
        self.start_time = None
        self.running = False

    def start(self):
        self.start_time = time.time()
        self.running = True
        self._tick()

    def _tick(self):
        if not self.running:
            return

        elapsed = time.time() - self.start_time
        remaining = max(0, self.duration - elapsed)

        if remaining <= 0:
            self._on_complete()
            return

        minutes = int(remaining) // 60
        seconds = int(remaining) % 60
        self._update_display(minutes, seconds, remaining)

        # 次のtickを壁時計に合わせてスケジュール
        # 秒の変わり目にできるだけ近いタイミングで更新
        next_second = int(elapsed) + 1
        delay_ms = max(50, int((next_second - elapsed) * 1000))
        self.root.after(delay_ms, self._tick)

    def pause(self):
        if self.running:
            self.running = False
            # 残り時間を保存
            elapsed = time.time() - self.start_time
            self.duration = max(0, self.duration - elapsed)

    def resume(self):
        if not self.running:
            self.start_time = time.time()
            self.running = True
            self._tick()
```

**ポイント:**
- `remaining` は毎回 `time.time()` から計算。`-= 1` のような累積計算をしない
- `after()` の間隔は表示更新の頻度であり、タイマーの正確性には影響しない
- スリープ復帰後、最初の `_tick()` 呼び出しで正しい残り時間が計算される
- `delay_ms` を壁時計の秒境界に合わせることで、表示のちらつきを最小化
- `max(50, ...)` で最小遅延を確保し、CPU負荷を防ぐ

### 一時停止/再開の仕組み

```
開始 → start_time = time.time(), duration = 1500
  ↓
一時停止 → duration = 1500 - elapsed (残り時間を保存)
  ↓
再開 → start_time = time.time() (新しい開始時刻, durationは残り時間)
```

---

## 6. 初期配置

### 画面サイズの取得

```python
root = tk.Tk()

screen_width = root.winfo_screenwidth()   # 1470
screen_height = root.winfo_screenheight() # 956
```

**注意:** `winfo_screenwidth/height` はメインディスプレイのサイズを返す。macOSのメニューバー（約25px）は含まれるため、実際に使える領域はやや小さい。

### 右上への配置

```python
def position_top_right(root, win_width, win_height, padding=20):
    """ウィンドウを画面右上に配置する"""
    screen_w = root.winfo_screenwidth()
    menu_bar_height = 25  # macOS メニューバーの高さ

    x = screen_w - win_width - padding
    y = menu_bar_height + padding

    root.geometry(f'{win_width}x{win_height}+{x}+{y}')
```

計算例 (1470x956, 200x200ウィンドウ, padding=20):
- x = 1470 - 200 - 20 = 1250
- y = 25 + 20 = 45
- → `200x200+1250+45`

### macOSノッチ対応

ノッチ付きMacBook（14/16インチ）ではメニューバーが37pxに拡大されるが、ウィンドウの配置にはシステムが自動的に対処する。`y = 45` は十分な余裕がある。

### マルチディスプレイ

```python
# tkinterの標準機能ではマルチディスプレイの個別情報取得は限定的
# 起動時はメインディスプレイの右上に配置し、
# ユーザーがドラッグで移動できるようにする

# winfo_screenwidth() はメインディスプレイのサイズを返す
# セカンダリディスプレイの座標は負の値やメイン画面サイズ以上の値になる
# ドラッグ移動で対応するのが最もシンプル
```

---

## 7. 実装テンプレート（全要素統合）

```python
import tkinter as tk
import time

class PomodoroOverlay:
    """macOS デスクトップ常駐ポモドーロタイマー"""

    def __init__(self):
        self.root = tk.Tk()
        self._setup_window()
        self._setup_canvas()
        self._setup_drag()

    def _setup_window(self):
        """透明・フレームレス・最前面ウィンドウの設定"""
        # フレームレス
        self.root.overrideredirect(True)

        # 透明背景（macOS専用）
        self.root.wm_attributes('-transparent', True)
        self.root.configure(bg='systemTransparent')

        # 常に最前面
        self.root.wm_attributes('-topmost', True)

        # 右上に配置
        self._position_top_right(200, 200)

    def _setup_canvas(self):
        """描画領域の設定"""
        self.canvas = tk.Canvas(
            self.root,
            width=200, height=200,
            bg='systemTransparent',
            highlightthickness=0
        )
        self.canvas.pack()

    def _setup_drag(self):
        """ドラッグ移動の設定"""
        self._drag_x = 0
        self._drag_y = 0
        self.canvas.bind('<Button-1>', self._start_drag)
        self.canvas.bind('<B1-Motion>', self._on_drag)

    def _start_drag(self, event):
        self._drag_x = event.x
        self._drag_y = event.y

    def _on_drag(self, event):
        x = self.root.winfo_x() + (event.x - self._drag_x)
        y = self.root.winfo_y() + (event.y - self._drag_y)
        self.root.geometry(f'+{x}+{y}')

    def _position_top_right(self, w, h, padding=20):
        screen_w = self.root.winfo_screenwidth()
        x = screen_w - w - padding
        y = 25 + padding  # メニューバー考慮
        self.root.geometry(f'{w}x{h}+{x}+{y}')

    def run(self):
        self.root.mainloop()
```

---

## 8. 既知の制限事項

| 項目 | 詳細 |
|------|------|
| フルスクリーンSpace | `-topmost` でもフルスクリーンアプリの上には表示されない |
| マルチディスプレイ | 初期配置はメインディスプレイのみ。ドラッグで移動可能 |
| Retinaディスプレイ | `winfo_screenwidth()` は論理ピクセルを返す。描画は自動でRetina対応 |
| overrideredirect | macOSではウィンドウシャドウが付かない。影が必要な場合はCanvas上に描画する |
| セキュリティ | スクリーン録画の権限は不要（自分のウィンドウを表示するだけ） |

---

## 9. Tk 9.0 macOS 対応属性一覧（検証済み）

```
-alpha           ウィンドウ全体の不透明度 (0.0-1.0)
-appearance      外観モード ('auto', 'aqua', 'darkaqua')
-buttons         ウィンドウボタン ('close', 'miniaturize', 'zoom')
-fullscreen      フルスクリーン (0/1)
-isdark          ダークモード判定 (読み取り専用)
-modified        ドキュメント変更マーカー
-notify          Dockアイコンバウンス
-titlepath       タイトルバーのファイルパスアイコン
-topmost         常に最前面 (0/1)
-transparent     背景透明化 (0/1)
-stylemask       ウィンドウ装飾の制御
-class           ウィンドウクラス
-tabbingid       タブグループID
-tabbingmode     タブモード ('auto', 'preferred', 'disallowed')
-type            ウィンドウタイプ
```
