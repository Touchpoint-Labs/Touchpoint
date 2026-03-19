"""Minimal Win32 test app with native Edit controls for destructive tests.

Uses ctypes to create a real Win32 window with native controls that UIA
recognizes correctly:
- Edit control → Role.TEXT_FIELD
- Button control → Role.BUTTON (clickable)

Process name: python.exe
Window title: "TouchpointTestApp"
Set TOUCHPOINT_TEST_APP=python to use with destructive tests.
"""
import ctypes
import ctypes.wintypes as wt
import sys

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

# --- Win32 constants ---
CS_HREDRAW = 0x0002
CS_VREDRAW = 0x0001
WS_OVERLAPPEDWINDOW = 0x00CF0000
WS_VISIBLE = 0x10000000
WS_CHILD = 0x40000000
WS_BORDER = 0x00800000
WS_TABSTOP = 0x00010000
ES_AUTOHSCROLL = 0x0080
BS_PUSHBUTTON = 0x0000
CW_USEDEFAULT = 0x80000000
WM_DESTROY = 0x0002
COLOR_WINDOW = 5
IDC_ARROW = 32512

WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_long, wt.HWND, ctypes.c_uint, wt.WPARAM, wt.LPARAM,
)


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


def wnd_proc(hwnd, msg, wparam, lparam):
    if msg == WM_DESTROY:
        user32.PostQuitMessage(0)
        return 0
    return user32.DefWindowProcW(hwnd, msg, wparam, lparam)


def main():
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

    hwnd = user32.CreateWindowExW(
        0, class_name, "TouchpointTestApp",
        WS_OVERLAPPEDWINDOW | WS_VISIBLE,
        CW_USEDEFAULT, CW_USEDEFAULT, 420, 250,
        None, None, hinstance, None,
    )
    if not hwnd:
        sys.exit("CreateWindowExW failed")

    # Static label
    user32.CreateWindowExW(
        0, "STATIC", "Name:",
        WS_CHILD | WS_VISIBLE,
        20, 20, 60, 25,
        hwnd, None, hinstance, None,
    )

    # Edit control (single-line text field)
    user32.CreateWindowExW(
        0x0200,  # WS_EX_CLIENTEDGE
        "EDIT", "",
        WS_CHILD | WS_VISIBLE | WS_TABSTOP | WS_BORDER | ES_AUTOHSCROLL,
        90, 18, 290, 25,
        hwnd, None, hinstance, None,
    )

    # Button
    user32.CreateWindowExW(
        0, "BUTTON", "Submit",
        WS_CHILD | WS_VISIBLE | WS_TABSTOP | BS_PUSHBUTTON,
        90, 60, 120, 30,
        hwnd, None, hinstance, None,
    )

    user32.ShowWindow(hwnd, 1)
    user32.UpdateWindow(hwnd)

    # Message loop
    msg = wt.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))


if __name__ == "__main__":
    main()
