"""LuckyLook popup. Press Ctrl+Alt+L anywhere in Windows to ask about your PC.

Run:  pythonw lucky.py      (no console window; errors go to lucky.log)
"""
import ctypes
import os
import queue
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
from ctypes import wintypes
from pathlib import Path

HERE = Path(__file__).parent
if sys.stderr is None:  # pythonw has no console
    sys.stdout = sys.stderr = open(HERE / "lucky.log", "a", encoding="utf-8", buffering=1)

import agent  # noqa: E402
import indexer  # noqa: E402

MOD_ALT, MOD_CONTROL, MOD_NOREPEAT, WM_HOTKEY = 0x1, 0x2, 0x4000, 0x312
HOTKEY = (MOD_CONTROL | MOD_ALT, ord("L"))  # Ctrl+Alt+L  (Ctrl+Alt+Space is taken by another app here)
HOTKEY_NAME = "Ctrl+Alt+L"
REINDEX_HOURS = 6  # full pass: the safety net for anything the live updates miss (and app/system folders)
LIVE_SECONDS = 5  # how soon a file you save, rename or delete shows up in answers
KEEP_HISTORY = 12  # messages of earlier conversation sent along with each question
WIDGET = True  # tab hanging off the top of the screen: hover pulls it down, click opens the popup (/widget hides it)
PEEK = 6  # pixels of the tab left on screen while it's tucked away
GLASS = "#141416"  # see-through: anything this colour in the popup shows the frosted glass instead
PANEL, FG, DIM, LINK, SEP, SELECT = "#22252c", "#f5f5f7", "#8e8e93", "#4da3ff", "#3a3a3f", "#264f78"
GLASS_MS = 100  # how often the glass catches up with what's behind it while open
FROST = bytes(int(v * 0.26 + 20) for v in range(256))  # darkens the blurred screen: white becomes grey 86
EDGE = 0x00423C3C  # hairline border, 0x00BBGGRR
FONT, TEXT_FONT = "Segoe UI Variable Display", "Segoe UI Variable Text"
# a path in backticks (may contain spaces), or a bare path (no spaces)
PATH_RE = re.compile(r"`([A-Za-z]:\\[^`\n]*)`|\b([A-Za-z]:\\[^\s`\"'<>|*?]*)")


class Popup:
    def __init__(self):
        self.q = queue.Queue()
        self.history = []
        self.busy = False
        self.links = 0
        r = self.root = tk.Tk()
        r.title("LuckyLook")
        r.iconbitmap(default=HERE / "assets" / "logo.ico")  # every LuckyLook window, error boxes included
        self.logo34 = tk.PhotoImage(file=HERE / "assets" / "logo-34.png")
        self.logo20 = tk.PhotoImage(file=HERE / "assets" / "logo-20.png")
        r.overrideredirect(True)
        r.attributes("-topmost", True)
        r.configure(bg=GLASS)
        r.attributes("-transparentcolor", GLASS)
        w, h = 760, 460
        self.box = ((r.winfo_screenwidth() - w) // 2, r.winfo_screenheight() // 6, w, h)
        r.geometry("{2}x{3}+{0}+{1}".format(*self.box))
        # the frosted glass: a window just under the popup showing the blurred screen, seen through the popup's
        # GLASS-coloured parts. Clicks and scrolls on those parts land here, so pass them back.
        self.back = tk.Toplevel(r)
        self.back.overrideredirect(True)
        self.back.attributes("-topmost", True)
        self.back.geometry("{2}x{3}+{0}+{1}".format(*self.box))
        self.backdrop = tk.Label(self.back, bd=0)
        self.backdrop.pack(fill="both", expand=True)
        self.backdrop.bind("<Button-1>", lambda e: self.entry.focus_force())
        self.backdrop.bind("<MouseWheel>", lambda e: self.out.yview_scroll(-e.delta // 120, "units"))
        self.back.withdraw()
        self.glass_job = None

        # Spotlight-style: big borderless search line on the glass, a hairline, then the answer
        top = tk.Frame(r, bg=GLASS)
        top.pack(fill="x", padx=18, pady=(14, 12))
        tk.Label(top, image=self.logo34, bg=GLASS).pack(side="left", padx=(0, 10))
        self.entry = tk.Entry(top, font=(FONT, 18), bg=GLASS, fg=FG, insertbackground=FG, relief="flat",
                              highlightthickness=0, selectbackground=SELECT)
        self.entry.pack(side="left", fill="x", expand=True)
        self.hint = tk.Label(top, text="Ask about this PC…", font=(FONT, 18), fg=DIM, bg=GLASS, cursor="xterm")
        self.hint.bind("<Button-1>", lambda e: self.entry.focus_set())
        self.entry.bind("<KeyRelease>", self.show_hint)
        tk.Frame(r, bg=SEP, height=1).pack(fill="x")
        self.out = tk.Text(r, wrap="word", font=(TEXT_FONT, 11), bg=GLASS, fg=FG, relief="flat", highlightthickness=0,
                           state="disabled", cursor="arrow", padx=4, spacing1=3, selectbackground=SELECT,
                           height=1)  # grows to fill; its default 24 lines would push the status line out of sight
        self.out.pack(fill="both", expand=True, padx=14, pady=(10, 0))
        self.status = tk.Label(r, anchor="w", font=(TEXT_FONT, 9), bg=GLASS, fg=DIM)
        self.status.pack(fill="x", padx=18, pady=(2, 10))
        self.show_hint()

        self.out.tag_config("you", foreground=DIM)
        self.out.tag_config("path", foreground=LINK, underline=True)
        self.out.tag_config("bold", font=(TEXT_FONT, 11, "bold"))
        self.out.tag_config("code", font=("Consolas", 11), foreground="#ffd479")
        self.out.tag_bind("path", "<Enter>", lambda e: self.out.config(cursor="hand2"))
        self.out.tag_bind("path", "<Leave>", lambda e: self.out.config(cursor="arrow"))
        self.entry.bind("<Return>", self.submit)
        r.bind("<Escape>", lambda e: self.toggle())
        r.withdraw()
        self.write("Ask anything about this PC.   Enter: ask · Esc: hide · /new: fresh chat · "
                   "/widget: show or hide the top tab · /quit: exit\n"
                   "Click a path to open it, right-click to show it in Explorer.\n", "you")

        self.make_widget()
        self.tray = None  # made on the hotkey thread, whose message loop delivers its clicks
        threading.Thread(target=self.hotkey_loop, daemon=True).start()
        r.after(30, self.pump)

    # --- background threads (talk to the UI only through self.q) ---

    def hotkey_loop(self):
        user32 = ctypes.windll.user32
        if not user32.RegisterHotKey(None, 1, HOTKEY[0] | MOD_NOREPEAT, HOTKEY[1]):
            self.q.put(("fatal", f"{HOTKEY_NAME} is already in use (LuckyLook already running, or another app). Change HOTKEY in lucky.py."))
            return
        threading.Thread(target=self.index_loop, daemon=True).start()
        self.tray = Tray(self.q)
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY:
                self.q.put(("toggle", None))
            else:
                user32.DispatchMessageW(ctypes.byref(msg))  # tray clicks

    def index_loop(self):
        """A full pass now and every REINDEX_HOURS; in between, file changes reach the index within LIVE_SECONDS."""
        changes = indexer.Watcher(indexer.ROOTS)  # before the full pass, so changes made during it aren't missed
        while True:
            next_full = time.time() + REINDEX_HOURS * 3600
            try:
                indexer.index(log=lambda m: self.q.put(("status", m)))
            except Exception as e:  # e.g. indexer.py is running by hand right now
                print(f"index error: {e}", flush=True)  # lands in lucky.log
                self.q.put(("status", f"index error: {e} (retrying in 10 min)"))
                next_full = time.time() + 600
            while time.time() < next_full:
                time.sleep(LIVE_SECONDS)
                paths = changes.take()
                if paths is None:  # Windows dropped some changes: only a full pass can catch up
                    break
                if paths:
                    try:
                        indexer.update(paths, log=lambda m: None)
                    except RuntimeError:  # indexer.py running by hand: apply these when it's done
                        changes.give_back(paths)
                    except Exception as e:
                        print(f"live index error: {e}", flush=True)

    def answer(self, question, start):
        try:
            reply = agent.ask(question, self.history, on_text=lambda t: self.q.put(("text", t)),
                              on_tool=lambda n, a: self.q.put(("status", f"⚙ {n} {a}")))
            self.history += [{"role": "user", "content": question}, {"role": "assistant", "content": reply}]
            del self.history[:-KEEP_HISTORY]
        except Exception as e:
            self.q.put(("text", f"\n⚠ {e}"))
        self.q.put(("done", start))

    # --- UI thread ---

    def make_widget(self):
        """A tab hanging off the top centre of the screen: hover pulls it down, click opens the popup."""
        w = self.widget = tk.Toplevel(self.root, bg=PANEL)
        w.overrideredirect(True)
        w.attributes("-topmost", True, "-toolwindow", True)  # tool window: no taskbar button
        # pady = PEEK, so while tucked away only padding shows, never a sliver of text
        self.tab = tk.Label(w, text=" LuckyLook", image=self.logo20, compound="left", font=(FONT, 10), fg=FG, padx=14,
                            pady=PEEK, cursor="hand2")
        hidden = 10  # extra top that always stays above the screen edge, so the tab looks attached to it
        self.tab.pack(pady=(hidden, 0))
        self.tab.bind("<Enter>", lambda e: self.pull_tab(True))
        self.tab.bind("<Leave>", lambda e: self.pull_tab(False))
        self.tab.bind("<Button-1>", lambda e: self.toggle())
        w.update_idletasks()
        area = wintypes.RECT()
        ctypes.windll.user32.SystemParametersInfoW(0x30, 0, ctypes.byref(area), 0)  # screen minus taskbar
        self.tab_x = (area.left + area.right - w.winfo_reqwidth()) // 2
        self.tab_off = area.top - w.winfo_reqheight()  # all of it above the screen
        self.tab_up = self.tab_off + PEEK
        self.tab_down = area.top - hidden
        self.tab_y, self.slide_job = self.tab_up, None
        self.widget_on, self.tab_gone = WIDGET, False
        w.geometry(f"+{self.tab_x}+{self.tab_y}")
        self.pull_tab(False)
        round_corners(w)
        self.watch_fullscreen()

    def watch_fullscreen(self):
        """Every second: while a full-screen app (video, game, slides) is in front, or after /widget, the tab goes
        all the way up out of sight. (Moved, not hidden: showing a window again can steal the keyboard.)"""
        state = ctypes.c_int()
        ctypes.windll.shell32.SHQueryUserNotificationState(ctypes.byref(state))
        gone = not self.widget_on or state.value in (2, 3, 4)  # full-screen app, full-screen game, presentation mode
        if gone != self.tab_gone:
            self.tab_gone = gone
            self.pull_tab(False)
        self.root.after(1000, self.watch_fullscreen)

    def pull_tab(self, down):
        self.tab.config(bg=PANEL if down else LINK)  # tucked away, it's a thin blue edge
        self.widget.attributes("-alpha", 1.0 if down else 0.6)
        self.slide(self.tab_down if down else self.tab_off if self.tab_gone else self.tab_up)

    def slide(self, target):
        """Moves the tab halfway to target each frame, so it eases into place."""
        if self.slide_job:
            self.root.after_cancel(self.slide_job)
            self.slide_job = None
        if self.tab_y != target:
            diff = target - self.tab_y
            self.tab_y += diff // 2 if abs(diff) > 1 else diff
            self.widget.geometry(f"+{self.tab_x}+{self.tab_y}")
            self.slide_job = self.root.after(15, self.slide, target)

    def show_hint(self, _=None):
        """Grey placeholder text while the question line is empty."""
        if self.entry.get():
            self.hint.place_forget()
        else:
            self.hint.place(in_=self.entry, x=1, rely=0.5, anchor="w")

    def pump(self):
        try:
            while True:
                kind, val = self.q.get_nowait()
                if kind == "toggle":
                    self.toggle()
                elif kind == "widget":
                    self.widget_on = not self.widget_on  # watch_fullscreen moves the tab within a second
                elif kind == "quit":
                    self.quit()
                    return
                elif kind == "text":
                    self.write(val)
                elif kind == "status":
                    self.status.config(text=val)
                elif kind == "done":
                    self.linkify(val)
                    render_markdown(self.out, val)
                    self.write("\n")
                    self.busy = False
                    self.status.config(text="")
                elif kind == "fatal":
                    from tkinter import messagebox
                    messagebox.showerror("LuckyLook", val)
                    self.root.destroy()
                    return
        except queue.Empty:
            pass
        self.root.after(30, self.pump)

    def quit(self):
        if self.tray:
            self.tray.remove()  # else a dead icon lingers in the tray until the mouse passes over it
        self.root.destroy()

    def toggle(self):
        r = self.root
        if r.state() == "withdrawn":
            self.frost_data = frosted(*self.box)
            self.frost = tk.PhotoImage(data=self.frost_data)
            self.backdrop.config(image=self.frost)
            self.back.deiconify()
            style(self.back)
            r.deiconify()
            hwnd = style(r)
            r.lift()
            r.focus_force()
            ctypes.windll.user32.SetForegroundWindow(hwnd)
            self.entry.focus_set()
            if self.glass_job:
                r.after_cancel(self.glass_job)
            self.glass_job = r.after(GLASS_MS, self.refresh_glass)
        else:
            r.withdraw()
            self.back.withdraw()

    def refresh_glass(self):
        """While the popup is open, keep its glass in step with what's moving behind it. Both windows are left out
        of screen captures, so the capture sees only what's behind them (~18 ms each time)."""
        self.glass_job = None
        if self.root.state() == "withdrawn":
            return
        data = frosted(*self.box)
        if data != self.frost_data:
            self.frost_data = data
            self.frost.configure(data=data)
        self.glass_job = self.root.after(GLASS_MS, self.refresh_glass)

    def write(self, text, tag=None):
        self.out.config(state="normal")
        self.out.insert("end", text, tag)
        self.out.config(state="disabled")
        self.out.see("end")

    def submit(self, _=None):
        question = self.entry.get().strip()
        if not question or self.busy:
            return
        self.entry.delete(0, "end")
        if question == "/quit":
            self.quit()
            return
        if question == "/widget":
            self.widget_on = not self.widget_on  # watch_fullscreen moves the tab within a second
            return
        if question == "/new":
            self.history.clear()
            self.out.config(state="normal")
            self.out.delete("1.0", "end")
            self.out.config(state="disabled")
            return
        self.busy = True
        self.status.config(text="thinking…")
        self.write(f"\n› {question}\n", "you")
        start = self.out.index("end-1c")
        threading.Thread(target=self.answer, args=(question, start), daemon=True).start()

    def linkify(self, start):
        """Make paths that exist on disk in the answer clickable."""
        self.out.config(state="normal")
        # last first, so dropping backticks doesn't shift the paths still to do
        for path, offset in reversed(list(find_paths(self.out.get(start, "end")))):
            if not os.path.exists(path):
                continue
            a = f"{start}+{offset}c"
            b = f"{a}+{len(path)}c"
            tag = f"link{self.links}"
            self.links += 1
            self.out.tag_add("path", a, b)
            self.out.tag_add(tag, a, b)
            self.out.tag_bind(tag, "<Button-1>", lambda e, p=path: os.startfile(p))
            self.out.tag_bind(tag, "<Button-3>", lambda e, p=path: subprocess.Popen(["explorer", "/select,", p]))
            if self.out.get(f"{a}-1c") == "`" and self.out.get(b) == "`":  # the link is enough, hide the backticks
                self.out.delete(b)
                self.out.delete(f"{a}-1c")
        self.out.config(state="disabled")


def round_corners(win):
    """Rounded corners on Windows 11 (does nothing on Windows 10)."""
    hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
    ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(ctypes.c_int(2)), 4)  # corner pref = round


def style(win):
    """Rounded corners, a hairline border, and left out of screenshots (so screen questions and the frost see what's
    behind it). Returns the window handle."""
    round_corners(win)
    hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
    ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 34, ctypes.byref(ctypes.c_int(EDGE)), 4)  # border colour
    ctypes.windll.user32.SetWindowDisplayAffinity(hwnd, 0x11)  # WDA_EXCLUDEFROMCAPTURE
    return hwnd


GDIPLUS = None  # started on first use


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("width", wintypes.LONG), ("height", wintypes.LONG),
                ("planes", wintypes.WORD), ("bits", wintypes.WORD), ("rest", wintypes.DWORD * 6)]


def frosted(x, y, w, h):
    """Apple-style frosted glass, done by hand because Windows' own acrylic can draw solid (it does on some PCs): a blurred,
    darkened copy of the screen at x, y, w, h, as PPM image data. Blur = shrink 40x, then stretch back, with GDI
    averaging the pixels on the way down and GDI+ smoothing them on the way up."""
    u, g, gp, H = ctypes.windll.user32, ctypes.windll.gdi32, ctypes.windll.gdiplus, ctypes.c_void_p
    for f in (u.GetDC, g.CreateCompatibleDC, g.CreateCompatibleBitmap, g.SelectObject):
        f.restype = H
    global GDIPLUS
    if not GDIPLUS:
        GDIPLUS = ctypes.c_size_t()
        gp.GdiplusStartup(ctypes.byref(GDIPLUS), ctypes.byref((ctypes.c_uint32 * 4)(1, 0, 0, 0)), None)
    k = 40  # blur strength: bigger = softer
    sw, sh = max(w // k, 1), max(h // k, 1)
    screen = H(u.GetDC(None))
    small_dc, big_dc = H(g.CreateCompatibleDC(screen)), H(g.CreateCompatibleDC(screen))
    small, big = H(g.CreateCompatibleBitmap(screen, sw, sh)), H(g.CreateCompatibleBitmap(screen, w, h))
    g.SelectObject(small_dc, small)
    g.SelectObject(big_dc, big)
    g.SetStretchBltMode(small_dc, 4)  # HALFTONE: average the pixels, don't just drop them
    g.StretchBlt(small_dc, 0, 0, sw, sh, screen, x, y, w, h, 0xCC0020)  # SRCCOPY
    g.DeleteDC(small_dc)  # GDI+ and GetDIBits want their bitmap out of any DC
    image, graphics = H(), H()
    gp.GdipCreateBitmapFromHBITMAP(small, None, ctypes.byref(image))
    gp.GdipCreateFromHDC(big_dc, ctypes.byref(graphics))
    gp.GdipSetInterpolationMode(graphics, 7)  # high-quality bicubic: GDI's own stretch-up is blocky
    gp.GdipDrawImageRectI(graphics, image, -k, -k, w + 2 * k, h + 2 * k)  # overdraw: the soft edges fall outside
    gp.GdipDeleteGraphics(graphics)
    gp.GdipDisposeImage(image)
    g.DeleteDC(big_dc)
    head = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32)  # negative height: rows top to bottom
    pixels = ctypes.create_string_buffer(w * h * 4)
    g.GetDIBits(screen, big, 0, h, pixels, ctypes.byref(head), 0)
    g.DeleteObject(small)
    g.DeleteObject(big)
    u.ReleaseDC(None, screen)
    bgra = pixels.raw.translate(FROST)
    rgb = bytearray(w * h * 3)
    rgb[0::3], rgb[1::3], rgb[2::3] = bgra[2::4], bgra[1::4], bgra[0::4]
    return b"P6 %d %d 255\n" % (w, h) + rgb


LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
WM_TRAY, WM_LBUTTONUP, WM_RBUTTONUP = 0x8001, 0x0202, 0x0205


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT), ("proc", WNDPROC), ("cls_extra", ctypes.c_int), ("wnd_extra", ctypes.c_int),
                ("instance", wintypes.HINSTANCE), ("icon", wintypes.HICON), ("cursor", wintypes.HANDLE),
                ("background", wintypes.HBRUSH), ("menu_name", wintypes.LPCWSTR), ("class_name", wintypes.LPCWSTR)]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("hwnd", wintypes.HWND), ("id", wintypes.UINT), ("flags", wintypes.UINT),
                ("callback", wintypes.UINT), ("icon", wintypes.HICON), ("tip", wintypes.WCHAR * 128),
                ("state", wintypes.DWORD), ("state_mask", wintypes.DWORD), ("info", wintypes.WCHAR * 256),
                ("version", wintypes.UINT), ("info_title", wintypes.WCHAR * 64), ("info_flags", wintypes.DWORD),
                ("guid", ctypes.c_byte * 16), ("balloon_icon", wintypes.HICON)]


class Tray:
    """LuckyLook's icon in the system tray. Click: open or close the popup. Right-click: a menu.
    Runs on the hotkey thread (its message loop delivers the clicks) and talks to the UI only through q."""
    MENU = [(1, f"Open LuckyLook\t{HOTKEY_NAME}", "toggle"), (2, "Show or hide the top tab", "widget"),
            (0, None, None), (3, "Quit LuckyLook", "quit")]

    def __init__(self, q):
        u, H = ctypes.windll.user32, ctypes.c_void_p
        u.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        u.DefWindowProcW.restype = LRESULT
        u.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD] + \
            [ctypes.c_int] * 4 + [wintypes.HWND, H, H, H]
        u.CreateWindowExW.restype = wintypes.HWND
        u.LoadImageW.restype = wintypes.HICON
        u.CreatePopupMenu.restype = H
        u.AppendMenuW.argtypes = [H, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
        u.TrackPopupMenu.argtypes = [H, wintypes.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND, H]
        u.DestroyMenu.argtypes = [H]
        self.q = q
        self.proc = WNDPROC(self.on_message)  # kept alive: Windows calls it as long as the window exists
        self.explorer_restarted = u.RegisterWindowMessageW("TaskbarCreated")  # before the window: it gets messages at once
        u.RegisterClassW(ctypes.byref(WNDCLASSW(proc=self.proc, class_name="LuckyLookTray")))
        self.hwnd = u.CreateWindowExW(0, "LuckyLookTray", "LuckyLook", 0, 0, 0, 0, 0, None, None, None, None)  # never shown
        icon = u.LoadImageW(None, str(HERE / "assets" / "logo.ico"), 1, u.GetSystemMetrics(49), u.GetSystemMetrics(50),
                            0x10)  # IMAGE_ICON, small-icon size, LR_LOADFROMFILE
        self.nid = NOTIFYICONDATAW(size=ctypes.sizeof(NOTIFYICONDATAW), hwnd=self.hwnd, id=1, flags=0x7,
                                   callback=WM_TRAY, icon=icon, tip=f"LuckyLook ({HOTKEY_NAME})")  # message+icon+tip
        self.add()

    def add(self):
        ctypes.windll.shell32.Shell_NotifyIconW(0, ctypes.byref(self.nid))  # NIM_ADD

    def remove(self):
        ctypes.windll.shell32.Shell_NotifyIconW(2, ctypes.byref(self.nid))  # NIM_DELETE

    def on_message(self, hwnd, msg, wparam, lparam):
        if msg == WM_TRAY and lparam & 0xFFFF == WM_LBUTTONUP:
            self.q.put(("toggle", None))
        elif msg == WM_TRAY and lparam & 0xFFFF == WM_RBUTTONUP:
            self.menu()
        elif msg == self.explorer_restarted:  # Explorer crashed or restarted: the icon must be added again
            self.add()
        return ctypes.windll.user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def menu(self):
        u = ctypes.windll.user32
        m = u.CreatePopupMenu()
        for cid, text, _ in self.MENU:
            u.AppendMenuW(m, 0x800 if not cid else 0, cid, text)  # 0x800: separator
        pt = wintypes.POINT()
        u.GetCursorPos(ctypes.byref(pt))
        u.SetForegroundWindow(self.hwnd)  # else the menu doesn't close when you click elsewhere
        choice = u.TrackPopupMenu(m, 0x180, pt.x, pt.y, 0, self.hwnd, None)  # return the choice, don't notify
        u.DestroyMenu(m)
        for cid, _, kind in self.MENU:
            if cid and cid == choice:
                self.q.put((kind, None))


MARKDOWN = re.compile(r"\*\*(.+?)\*\*|`([^`\n]+)`|^(#{1,4} |[-*] )", re.M)


def render_markdown(text, start):
    """Shows the answer's light markdown as styles: **bold**, `code`, # headings (bold), - bullets (•).
    Works on the finished answer, from start to the end of the Text widget."""
    state = text.cget("state")
    text.config(state="normal")
    for m in reversed(list(MARKDOWN.finditer(text.get(start, "end")))):  # last first: earlier offsets stay right
        a = f"{start}+{m.start()}c"
        if m[3] and m[3][0] == "#":
            text.tag_add("bold", f"{a}+{len(m[3])}c", f"{a} lineend")
            text.delete(a, f"{a}+{len(m[3])}c")
        elif m[3]:
            text.delete(a, f"{a}+1c")
            text.insert(a, "•")
        else:
            inner, mark = (m[1], 2) if m[1] else (m[2], 1)
            text.tag_add("bold" if m[1] else "code", f"{a}+{mark}c", f"{a}+{mark + len(inner)}c")
            text.delete(f"{a}+{mark + len(inner)}c", f"{a}+{2 * mark + len(inner)}c")
            text.delete(a, f"{a}+{mark}c")
    text.config(state=state)


def find_paths(text):
    """Yields (path, offset) for Windows paths in text."""
    for m in PATH_RE.finditer(text):
        if m[1]:
            yield m[1].strip(), m.start(1)
        else:
            yield m[2].rstrip(".,;:)]"), m.start(2)


if __name__ == "__main__":
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # crisp text on high-DPI screens
    except Exception:
        pass
    Popup().root.mainloop()
