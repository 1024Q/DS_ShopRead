"""Windows DPAPI (current user) protection for small local secrets."""

import base64
import ctypes
import sys


def available():
    return sys.platform == "win32"


def protect(value, decode=False):
    """protect(text) -> base64 blob; protect(blob, True) -> text. Bound to this Windows account."""
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("length", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_char))]

    raw = base64.b64decode(value, validate=True) if decode else value.encode("utf-8")
    buffer = ctypes.create_string_buffer(raw)
    source = Blob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    out = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    fn = crypt.CryptUnprotectData if decode else crypt.CryptProtectData
    if not fn(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(out)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        result = ctypes.string_at(out.data, out.length)
        return result.decode("utf-8") if decode else base64.b64encode(result).decode("ascii")
    finally:
        kernel.LocalFree(out.data)
