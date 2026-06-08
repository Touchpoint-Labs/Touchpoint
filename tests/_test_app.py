"""Deterministic Win32 control gallery for live Windows UIA tests.

Uses only ``ctypes`` and controls shipped with Windows.  The window exposes
representative provider families for role, state, action, text, and window
management validation:

- classic Edit controls: single-line, multiline, and read-only
- Rich Edit text
- push button, checkbox, and radio buttons
- combo box, multi-select list box, tree view, and tab control
- trackbar slider and progress bar

Process name: python.exe
Window title: "TouchpointTestApp"
Set TOUCHPOINT_TEST_APP=python to use with the shared destructive tests.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import sys


user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
comctl32 = ctypes.windll.comctl32

kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
kernel32.GetModuleHandleW.restype = wt.HMODULE
kernel32.LoadLibraryW.argtypes = [wt.LPCWSTR]
kernel32.LoadLibraryW.restype = wt.HMODULE
user32.CreateWindowExW.argtypes = [
    wt.DWORD,
    wt.LPCWSTR,
    wt.LPCWSTR,
    wt.DWORD,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wt.HWND,
    wt.HMENU,
    wt.HINSTANCE,
    wt.LPVOID,
]
user32.CreateWindowExW.restype = wt.HWND
user32.DefWindowProcW.argtypes = [
    wt.HWND,
    ctypes.c_uint,
    wt.WPARAM,
    wt.LPARAM,
]
user32.DefWindowProcW.restype = wt.LPARAM
user32.LoadCursorW.restype = wt.HCURSOR
user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
user32.ShowWindow.restype = wt.BOOL
user32.UpdateWindow.argtypes = [wt.HWND]
user32.UpdateWindow.restype = wt.BOOL

# Window styles
CS_HREDRAW = 0x0002
CS_VREDRAW = 0x0001
WS_OVERLAPPEDWINDOW = 0x00CF0000
WS_VISIBLE = 0x10000000
WS_CHILD = 0x40000000
WS_BORDER = 0x00800000
WS_TABSTOP = 0x00010000
WS_VSCROLL = 0x00200000
WS_EX_CLIENTEDGE = 0x0200

# Control styles
ES_MULTILINE = 0x0004
ES_AUTOHSCROLL = 0x0080
ES_AUTOVSCROLL = 0x0040
ES_READONLY = 0x0800
BS_PUSHBUTTON = 0x0000
BS_AUTOCHECKBOX = 0x0003
BS_AUTORADIOBUTTON = 0x0009
CBS_DROPDOWNLIST = 0x0003
CBS_HASSTRINGS = 0x0200
LBS_NOTIFY = 0x0001
LBS_EXTENDEDSEL = 0x0800
TBS_AUTOTICKS = 0x0001
TVS_HASBUTTONS = 0x0001
TVS_HASLINES = 0x0002
TVS_LINESATROOT = 0x0004

# Messages
WM_DESTROY = 0x0002
WM_USER = 0x0400
CB_ADDSTRING = 0x0143
CB_SETCURSEL = 0x014E
LB_ADDSTRING = 0x0180
LB_SETSEL = 0x0185
TBM_SETRANGE = WM_USER + 6
TBM_SETPOS = WM_USER + 5
PBM_SETRANGE32 = WM_USER + 6
PBM_SETPOS = WM_USER + 2
TCM_INSERTITEMW = 0x133E
TVM_INSERTITEMW = 0x1132

# Common controls
ICC_TREEVIEW_CLASSES = 0x00000002
ICC_BAR_CLASSES = 0x00000004
ICC_TAB_CLASSES = 0x00000008
ICC_PROGRESS_CLASS = 0x00000020

COLOR_WINDOW = 5
CW_USEDEFAULT = 0x80000000
IDC_ARROW = 32512
TCIF_TEXT = 0x0001
TVIF_TEXT = 0x0001
TVI_ROOT = -0x10000
TVI_LAST = -0x0FFFE

WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_longlong,
    wt.HWND,
    ctypes.c_uint,
    wt.WPARAM,
    wt.LPARAM,
)

user32.SendMessageW.argtypes = [
    wt.HWND,
    ctypes.c_uint,
    wt.WPARAM,
    wt.LPARAM,
]
user32.SendMessageW.restype = wt.LPARAM


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", ctypes.c_uint),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wt.HINSTANCE),
        ("hIcon", wt.HICON),
        ("hCursor", wt.HANDLE),
        ("hbrBackground", wt.HBRUSH),
        ("lpszMenuName", wt.LPCWSTR),
        ("lpszClassName", wt.LPCWSTR),
    ]


class INITCOMMONCONTROLSEX(ctypes.Structure):
    _fields_ = [
        ("dwSize", wt.DWORD),
        ("dwICC", wt.DWORD),
    ]


class TCITEMW(ctypes.Structure):
    _fields_ = [
        ("mask", ctypes.c_uint),
        ("dwState", wt.DWORD),
        ("dwStateMask", wt.DWORD),
        ("pszText", wt.LPWSTR),
        ("cchTextMax", ctypes.c_int),
        ("iImage", ctypes.c_int),
        ("lParam", wt.LPARAM),
    ]


class TVITEMW(ctypes.Structure):
    _fields_ = [
        ("mask", ctypes.c_uint),
        ("hItem", wt.HANDLE),
        ("state", ctypes.c_uint),
        ("stateMask", ctypes.c_uint),
        ("pszText", wt.LPWSTR),
        ("cchTextMax", ctypes.c_int),
        ("iImage", ctypes.c_int),
        ("iSelectedImage", ctypes.c_int),
        ("cChildren", ctypes.c_int),
        ("lParam", wt.LPARAM),
    ]


class TVINSERTSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hParent", wt.HANDLE),
        ("hInsertAfter", wt.HANDLE),
        ("item", TVITEMW),
    ]


def wnd_proc(hwnd, msg, wparam, lparam):
    if msg == WM_DESTROY:
        user32.PostQuitMessage(0)
        return 0
    return user32.DefWindowProcW(hwnd, msg, wparam, lparam)


def _send(hwnd, msg, wparam=0, lparam=0):
    return user32.SendMessageW(hwnd, msg, wparam, lparam)


def _add_string(hwnd, msg, value):
    text = ctypes.create_unicode_buffer(value)
    return _send(hwnd, msg, 0, ctypes.addressof(text))


def _create(
    hinstance,
    parent,
    class_name,
    text,
    style,
    x,
    y,
    width,
    height,
    *,
    ex_style=0,
):
    hwnd = user32.CreateWindowExW(
        ex_style,
        class_name,
        text,
        style,
        x,
        y,
        width,
        height,
        parent,
        None,
        hinstance,
        None,
    )
    if not hwnd:
        raise OSError(f"CreateWindowExW failed for {class_name!r}")
    return hwnd


def _init_common_controls():
    config = INITCOMMONCONTROLSEX()
    config.dwSize = ctypes.sizeof(config)
    config.dwICC = (
        ICC_TREEVIEW_CLASSES
        | ICC_BAR_CLASSES
        | ICC_TAB_CLASSES
        | ICC_PROGRESS_CLASS
    )
    if not comctl32.InitCommonControlsEx(ctypes.byref(config)):
        raise OSError("InitCommonControlsEx failed")


def _insert_tab(tab_hwnd, index, title):
    text = ctypes.create_unicode_buffer(title)
    item = TCITEMW()
    item.mask = TCIF_TEXT
    item.pszText = ctypes.cast(text, wt.LPWSTR)
    _send(tab_hwnd, TCM_INSERTITEMW, index, ctypes.addressof(item))


def _insert_tree_item(tree_hwnd, title, parent=TVI_ROOT):
    text = ctypes.create_unicode_buffer(title)
    item = TVINSERTSTRUCTW()
    item.hParent = parent
    item.hInsertAfter = TVI_LAST
    item.item.mask = TVIF_TEXT
    item.item.pszText = ctypes.cast(text, wt.LPWSTR)
    return _send(tree_hwnd, TVM_INSERTITEMW, 0, ctypes.addressof(item))


def main():
    _init_common_controls()
    kernel32.LoadLibraryW("Msftedit.dll")

    hinstance = kernel32.GetModuleHandleW(None)
    class_name = "TouchpointTestClass"

    wc = WNDCLASSW()
    wc.style = CS_HREDRAW | CS_VREDRAW
    wc.lpfnWndProc = WNDPROC(wnd_proc)
    wc.hInstance = hinstance
    wc.hCursor = user32.LoadCursorW(None, IDC_ARROW)
    wc.hbrBackground = ctypes.cast(COLOR_WINDOW + 1, wt.HBRUSH)
    wc.lpszClassName = class_name

    if not user32.RegisterClassW(ctypes.byref(wc)):
        sys.exit("RegisterClassW failed")

    hwnd = _create(
        hinstance,
        None,
        class_name,
        "TouchpointTestApp",
        WS_OVERLAPPEDWINDOW | WS_VISIBLE,
        CW_USEDEFAULT,
        CW_USEDEFAULT,
        980,
        720,
    )

    _create(
        hinstance, hwnd, "STATIC", "Single line:",
        WS_CHILD | WS_VISIBLE, 20, 20, 100, 25,
    )
    _create(
        hinstance, hwnd, "EDIT", "Alpha Beta Gamma",
        WS_CHILD | WS_VISIBLE | WS_TABSTOP | WS_BORDER | ES_AUTOHSCROLL,
        130, 18, 300, 25, ex_style=WS_EX_CLIENTEDGE,
    )

    _create(
        hinstance, hwnd, "STATIC", "Multiline:",
        WS_CHILD | WS_VISIBLE, 20, 60, 100, 25,
    )
    _create(
        hinstance, hwnd, "EDIT", "Line one\r\nLine two",
        (
            WS_CHILD | WS_VISIBLE | WS_TABSTOP | WS_BORDER | WS_VSCROLL
            | ES_MULTILINE | ES_AUTOVSCROLL
        ),
        130, 58, 300, 80, ex_style=WS_EX_CLIENTEDGE,
    )

    _create(
        hinstance, hwnd, "STATIC", "Read only:",
        WS_CHILD | WS_VISIBLE, 20, 155, 100, 25,
    )
    _create(
        hinstance, hwnd, "EDIT", "Locked text",
        WS_CHILD | WS_VISIBLE | WS_BORDER | ES_AUTOHSCROLL | ES_READONLY,
        130, 153, 300, 25, ex_style=WS_EX_CLIENTEDGE,
    )

    _create(
        hinstance, hwnd, "STATIC", "Rich text:",
        WS_CHILD | WS_VISIBLE, 20, 195, 100, 25,
    )
    _create(
        hinstance, hwnd, "RICHEDIT50W", "Rich Alpha Beta",
        (
            WS_CHILD | WS_VISIBLE | WS_TABSTOP | WS_BORDER | WS_VSCROLL
            | ES_MULTILINE | ES_AUTOVSCROLL
        ),
        130, 193, 300, 70, ex_style=WS_EX_CLIENTEDGE,
    )

    _create(
        hinstance, hwnd, "BUTTON", "Submit",
        WS_CHILD | WS_VISIBLE | WS_TABSTOP | BS_PUSHBUTTON,
        20, 290, 120, 30,
    )
    _create(
        hinstance, hwnd, "BUTTON", "Enable feature",
        WS_CHILD | WS_VISIBLE | WS_TABSTOP | BS_AUTOCHECKBOX,
        165, 290, 150, 30,
    )
    _create(
        hinstance, hwnd, "BUTTON", "Choice A",
        WS_CHILD | WS_VISIBLE | WS_TABSTOP | BS_AUTORADIOBUTTON,
        335, 290, 100, 30,
    )
    _create(
        hinstance, hwnd, "BUTTON", "Choice B",
        WS_CHILD | WS_VISIBLE | WS_TABSTOP | BS_AUTORADIOBUTTON,
        335, 325, 100, 30,
    )

    _create(
        hinstance, hwnd, "STATIC", "Plan:",
        WS_CHILD | WS_VISIBLE, 20, 350, 100, 25,
    )
    combo = _create(
        hinstance, hwnd, "COMBOBOX", "",
        (
            WS_CHILD | WS_VISIBLE | WS_TABSTOP | WS_VSCROLL
            | CBS_DROPDOWNLIST | CBS_HASSTRINGS
        ),
        130, 348, 200, 120,
    )
    for value in ("Basic", "Pro", "Enterprise"):
        _add_string(combo, CB_ADDSTRING, value)
    _send(combo, CB_SETCURSEL, 1, 0)

    _create(
        hinstance, hwnd, "STATIC", "Items:",
        WS_CHILD | WS_VISIBLE, 20, 395, 100, 25,
    )
    listbox = _create(
        hinstance, hwnd, "LISTBOX", "",
        (
            WS_CHILD | WS_VISIBLE | WS_TABSTOP | WS_BORDER | WS_VSCROLL
            | LBS_NOTIFY | LBS_EXTENDEDSEL
        ),
        130, 393, 200, 90, ex_style=WS_EX_CLIENTEDGE,
    )
    for value in ("First item", "Second item", "Third item"):
        _add_string(listbox, LB_ADDSTRING, value)
    _send(listbox, LB_SETSEL, 1, 1)

    _create(
        hinstance, hwnd, "STATIC", "Volume:",
        WS_CHILD | WS_VISIBLE, 20, 505, 100, 25,
    )
    slider = _create(
        hinstance, hwnd, "msctls_trackbar32", "",
        WS_CHILD | WS_VISIBLE | WS_TABSTOP | TBS_AUTOTICKS,
        130, 498, 300, 40,
    )
    _send(slider, TBM_SETRANGE, 1, (100 << 16) | 0)
    _send(slider, TBM_SETPOS, 1, 40)

    _create(
        hinstance, hwnd, "STATIC", "Progress:",
        WS_CHILD | WS_VISIBLE, 20, 550, 100, 25,
    )
    progress = _create(
        hinstance, hwnd, "msctls_progress32", "",
        WS_CHILD | WS_VISIBLE, 130, 552, 300, 20,
    )
    _send(progress, PBM_SETRANGE32, 0, 100)
    _send(progress, PBM_SETPOS, 65, 0)

    tabs = _create(
        hinstance, hwnd, "SysTabControl32", "",
        WS_CHILD | WS_VISIBLE | WS_TABSTOP,
        500, 20, 420, 140,
    )
    _insert_tab(tabs, 0, "Overview")
    _insert_tab(tabs, 1, "Details")

    tree = _create(
        hinstance, hwnd, "SysTreeView32", "",
        (
            WS_CHILD | WS_VISIBLE | WS_TABSTOP | WS_BORDER
            | TVS_HASBUTTONS | TVS_HASLINES | TVS_LINESATROOT
        ),
        500, 190, 420, 240, ex_style=WS_EX_CLIENTEDGE,
    )
    root_item = _insert_tree_item(tree, "Root item")
    _insert_tree_item(tree, "Child item", parent=root_item)

    user32.ShowWindow(hwnd, 1)
    user32.UpdateWindow(hwnd)

    msg = wt.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))


if __name__ == "__main__":
    main()
