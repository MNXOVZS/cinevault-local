"""[INPUT]: Windows 桌面与本机服务的停止信号。
[OUTPUT]: 选中的文件夹路径，或取消结果。
[POS]: 在独立进程运行系统对话框，限制并发与等待时间。
[PROTOCOL]: Update this header on change, then check AGENTS.md
"""
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid

AVAILABLE = os.name == 'nt'
_LOCK = threading.Lock()
_DIALOG_SECONDS = 120


def choose_folder(stop):
    if not AVAILABLE:
        raise NotImplementedError('此平台请手动输入目录路径。')
    if not _LOCK.acquire(blocking=False):
        raise BlockingIOError('文件夹选择窗口已打开，请先完成或取消。')
    try:
        executable = Path(sys.executable)
        console = executable.with_name('python.exe')
        if executable.name.lower() == 'pythonw.exe' and console.is_file():
            executable = console
        command = [str(executable), '-I', '-X', 'utf8', str(Path(__file__).resolve())]
        with subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW) as process:
            try:
                deadline = time.monotonic() + _DIALOG_SECONDS
                while not stop.is_set() and time.monotonic() < deadline:
                    try:
                        output, _ = process.communicate(timeout=.5)
                    except subprocess.TimeoutExpired:
                        continue
                    if process.returncode == 0:
                        return json.loads(output)
                    raise OSError('无法启动系统文件夹选择窗口。')
                raise TimeoutError('已关闭等待超时的文件夹选择窗口。')
            finally:
                if process.poll() is None:
                    process.kill()
    finally:
        _LOCK.release()


def _windows_dialog():
    from ctypes import wintypes

    # Windows SDK: IUnknown, IModalWindow, IFileDialog and IShellItem slots.
    release, show, set_options, set_title, get_result, get_display_name = 2, 3, 9, 17, 20, 5
    apartment_threaded, inproc_server = 2, 1
    no_change_dir, pick_folders, filesystem = 0x8, 0x20, 0x40
    path_must_exist, no_recent = 0x800, 0x02000000
    filesystem_path, cancelled = 0x80058000, 0x800704C7
    hresult = ctypes.c_long
    pointer = ctypes.c_void_p
    pointer_out = ctypes.POINTER(pointer)
    guid = ctypes.c_ubyte * 16
    clsid = guid.from_buffer_copy(uuid.UUID('dc1c5a9c-e88a-4dde-a5a1-60f82a20aef7').bytes_le)
    iid = guid.from_buffer_copy(uuid.UUID('42f85136-db7e-439c-85f1-e4075d135fc8').bytes_le)
    ole = ctypes.WinDLL('ole32')
    ole.CoInitializeEx.argtypes = (pointer, wintypes.DWORD)
    ole.CoInitializeEx.restype = hresult
    ole.CoUninitialize.argtypes = ()
    ole.CoUninitialize.restype = None
    ole.CoCreateInstance.argtypes = (ctypes.POINTER(guid), pointer, wintypes.DWORD, ctypes.POINTER(guid), pointer_out)
    ole.CoCreateInstance.restype = hresult
    ole.CoTaskMemFree.argtypes = (pointer,)
    ole.CoTaskMemFree.restype = None
    def check(code):
        if code < 0:
            raise OSError(f'Windows 目录选择错误 0x{code & 0xffffffff:08X}')

    def method(instance, slot, result, *args):
        table = ctypes.cast(instance, ctypes.POINTER(ctypes.POINTER(pointer))).contents
        return ctypes.WINFUNCTYPE(result, pointer, *args)(table[slot])

    dialog, item, chosen = pointer(), pointer(), pointer()
    check(ole.CoInitializeEx(None, apartment_threaded))
    try:
        check(ole.CoCreateInstance(ctypes.byref(clsid), None, inproc_server, ctypes.byref(iid), ctypes.byref(dialog)))
        options = no_change_dir | pick_folders | filesystem | path_must_exist | no_recent
        check(method(dialog, set_options, hresult, ctypes.c_uint)(dialog, options))
        check(method(dialog, set_title, hresult, ctypes.c_wchar_p)(dialog, '映库 · 选择媒体文件夹'))
        # 不锁定当前前台应用。浏览器与选择器属于不同进程。
        code = method(dialog, show, hresult, wintypes.HWND)(dialog, None)
        if code & 0xffffffff == cancelled:
            return None
        check(code)
        check(method(dialog, get_result, hresult, pointer_out)(dialog, ctypes.byref(item)))
        check(method(item, get_display_name, hresult, ctypes.c_uint, pointer_out)(item, filesystem_path, ctypes.byref(chosen)))
        return ctypes.wstring_at(chosen)
    finally:
        if chosen:
            ole.CoTaskMemFree(chosen)
        if item:
            method(item, release, wintypes.ULONG)(item)
        if dialog:
            method(dialog, release, wintypes.ULONG)(dialog)
        ole.CoUninitialize()


if __name__ == '__main__':
    print(json.dumps(_windows_dialog()), flush=True)
