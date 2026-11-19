"""Bounded-strip RGB ICC conversion without an 8-bit intermediate.

This is an independent ctypes binding to LittleCMS 2's public C API. It calls
an installed shared library; it does not bundle LittleCMS or any application
DLL. LittleCMS is MIT licensed: https://github.com/mm2/Little-CMS/blob/master/LICENSE

``FloatICCTransform(profile_bytes).apply(strip)`` accepts normalized, encoded
RGB floating-point samples and returns encoded sRGB float32. The caller owns
alpha handling, orientation, and any destination-gamut clipping. In particular,
float transforms may return finite values outside [0, 1]. A profile can itself
contain sampled/quantized tables; float transport cannot undo that precision.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import hashlib
import json
import os
from pathlib import Path
import sys
import threading

import numpy as np


class ICCDependencyError(RuntimeError):
    code = "ICC_DEPENDENCY_MISSING"


class ICCProfileError(ValueError):
    code = "INVALID_ICC"


class ICCTransformError(RuntimeError):
    code = "ICC_TRANSFORM_FAILED"


# Public lcms2.h format bitfields: FLOAT_SH(1), COLORSPACE_SH(PT_RGB),
# CHANNELS_SH(3), BYTES_SH(4). These select actual 32-bit float samples.
TYPE_RGB_FLT = (1 << 22) | (4 << 16) | (3 << 3) | 4
_RGB = 0x52474220
_XYZ = 0x58595A20
_LAB = 0x4C616220
_LINK = 0x6C696E6B
_NAMED = 0x6E6D636C
_NOCACHE = 0x0040
_NOOPTIMIZE = 0x0100
_BPC = 0x2000
_ERROR_CALLBACK = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_char_p)
_LIBRARIES = {}
_LIBRARY_LOCK = threading.Lock()


def _known_windows_libraries():
    """Search bounded, known application directories, never an entire drive."""
    roots = []
    configured = os.environ.get("SPEKTRA_RUNTIME_ROOT")
    if configured:
        roots.append(Path(configured) / "apps")
    try:
        settings = json.loads((Path(__file__).resolve().parent.parent / "runtime.local.json").read_text(encoding="utf-8"))
        if settings.get("root"):
            roots.append(Path(settings["root"]) / "apps")
    except (OSError, ValueError, TypeError):
        pass
    roots.append(Path.home() / "Desktop" / "SpektraTest" / "apps")
    patterns = (
        "GIMP/GIMP-*/bin/liblcms2-2.dll",
        "GIMP/GIMP-*/bin/liblcms2.dll",
        "Krita/krita-*/bin/liblcms2.dll",
        "Krita/krita-*/bin/liblcms2-2.dll",
    )
    for root in dict.fromkeys(roots):
        if root.is_dir():
            for pattern in patterns:
                yield from sorted(root.glob(pattern), reverse=True)
    for name in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        base = os.environ.get(name)
        if not base:
            continue
        root = Path(base)
        for pattern in (
            "GIMP */bin/liblcms2-2.dll", "GIMP */bin/liblcms2.dll",
            "Krita*/bin/liblcms2.dll", "Krita*/bin/liblcms2-2.dll",
            "Programs/GIMP */bin/liblcms2-2.dll", "Programs/Krita*/bin/liblcms2.dll",
        ):
            yield from sorted(root.glob(pattern), reverse=True)


class _LCMS:
    def __init__(self, candidate):
        self.path = str(candidate)
        self.dll_directory = None
        path = Path(candidate)
        if os.name == "nt" and path.is_absolute() and hasattr(os, "add_dll_directory"):
            self.dll_directory = os.add_dll_directory(str(path.parent))
        try:
            # 64-bit Windows has one calling convention. Legacy 32-bit MSVC
            # LittleCMS builds export stdcall; Unix builds use the C ABI.
            loader = ctypes.WinDLL if os.name == "nt" and ctypes.sizeof(ctypes.c_void_p) == 4 else ctypes.CDLL
            options = {"winmode": 0x00000100 | 0x00001000} if os.name == "nt" and path.is_absolute() else {}
            self.lib = loader(self.path, **options)
            self._bind()
        except Exception:
            if self.dll_directory is not None:
                self.dll_directory.close()
            raise

    def _bind(self):
        ptr, u32 = ctypes.c_void_p, ctypes.c_uint32
        signatures = {
            "cmsGetEncodedCMMversion": ([], u32),
            "cmsCreateContext": ([ptr, ptr], ptr),
            "cmsDeleteContext": ([ptr], None),
            "cmsSetLogErrorHandlerTHR": ([ptr, _ERROR_CALLBACK], None),
            "cmsOpenProfileFromMemTHR": ([ptr, ptr, u32], ptr),
            "cmsCreate_sRGBProfileTHR": ([ptr], ptr),
            "cmsGetColorSpace": ([ptr], u32),
            "cmsGetPCS": ([ptr], u32),
            "cmsGetDeviceClass": ([ptr], u32),
            "cmsCloseProfile": ([ptr], ctypes.c_int),
            "cmsCreateTransformTHR": ([ptr, ptr, u32, ptr, u32, u32, u32], ptr),
            "cmsDeleteTransform": ([ptr], None),
            "cmsDoTransform": ([ptr, ptr, ptr, u32], None),
        }
        for name, (arguments, result) in signatures.items():
            fn = getattr(self.lib, name)
            fn.argtypes, fn.restype = arguments, result


def _load_library():
    override = os.environ.get("SPEKTRA_LCMS2")
    key = override or "<auto>"
    with _LIBRARY_LOCK:
        if key in _LIBRARIES:
            return _LIBRARIES[key]
        if override:
            candidates = [str(Path(override).expanduser().resolve())]
        else:
            discovered = ctypes.util.find_library("lcms2")
            candidates = [discovered] if discovered else []
            if os.name == "nt":
                candidates.extend(str(p) for p in _known_windows_libraries())
            elif sys.platform == "darwin":
                candidates.extend(["liblcms2.dylib", "/opt/homebrew/lib/liblcms2.dylib", "/usr/local/lib/liblcms2.dylib"])
            else:
                candidates.extend(["liblcms2.so.2", "liblcms2.so"])
        failures = []
        for candidate in dict.fromkeys(candidates):
            try:
                library = _LCMS(candidate)
                _LIBRARIES[key] = library
                return library
            except (OSError, AttributeError) as exc:
                failures.append(f"{candidate}: {exc}")
        detail = "; ".join(failures[:3]) or "no library found in the system or known application directories"
        raise ICCDependencyError(
            "LittleCMS 2 is required for non-sRGB ICC conversion without reducing to 8-bit. "
            "Install LittleCMS 2 (or an existing GIMP/Krita runtime), or set SPEKTRA_LCMS2 "
            f"to its shared-library path. {detail}"
        )


class FloatICCTransform:
    """Reusable per-profile, per-context float RGB-to-sRGB transform.

    The default rendering intent is perceptual (0); 1, 2, and 3 select relative
    colorimetric, saturation, and absolute colorimetric. All handles are closed
    by the context manager. Calls to one instance are serialized.
    """
    def __init__(self, icc_bytes, *, intent=0, black_point_compensation=False):
        self._lock = threading.RLock()
        self._context = self._source = self._target = self._transform = None
        self._library = self._buffer = self._callback = None
        self._error = None
        if isinstance(intent, bool) or intent not in (0, 1, 2, 3):
            raise ValueError("ICC rendering intent must be 0, 1, 2, or 3")
        if not isinstance(icc_bytes, (bytes, bytearray, memoryview)):
            raise ICCProfileError("ICC profile must be bytes")
        profile = bytes(icc_bytes)
        if len(profile) < 128 or profile[36:40] != b"acsp":
            raise ICCProfileError("ICC profile header is missing or invalid")
        declared = int.from_bytes(profile[:4], "big")
        if declared < 128 or declared > len(profile) or declared > 0xFFFFFFFF:
            raise ICCProfileError("ICC profile size is invalid or profile is truncated")
        if profile[16:20] != b"RGB ":
            raise ICCProfileError("Only RGB ICC profiles can describe RGB samples; CMYK, gray, and other color spaces are unsupported")
        try:
            self._library = _load_library()
            lib = self._library.lib
            self._context = lib.cmsCreateContext(None, None)
            if not self._context:
                raise ICCTransformError("LittleCMS could not allocate a context")
            self._callback = _ERROR_CALLBACK(self._record_error)
            lib.cmsSetLogErrorHandlerTHR(self._context, self._callback)
            self._buffer = ctypes.create_string_buffer(profile[:declared])
            self._source = lib.cmsOpenProfileFromMemTHR(self._context, self._buffer, declared)
            if not self._source:
                raise ICCProfileError(self._detail("LittleCMS rejected the ICC profile"))
            if lib.cmsGetColorSpace(self._source) != _RGB:
                raise ICCProfileError("ICC profile color space does not match RGB samples")
            if lib.cmsGetPCS(self._source) not in (_XYZ, _LAB) or lib.cmsGetDeviceClass(self._source) in (_LINK, _NAMED):
                raise ICCProfileError("An RGB source ICC profile with XYZ/Lab PCS is required; device-link and named-color profiles are unsupported")
            self._target = lib.cmsCreate_sRGBProfileTHR(self._context)
            if not self._target:
                raise ICCTransformError(self._detail("LittleCMS could not create sRGB profile"))
            flags = _NOCACHE | _NOOPTIMIZE | (_BPC if black_point_compensation else 0)
            self._transform = lib.cmsCreateTransformTHR(
                self._context, self._source, TYPE_RGB_FLT, self._target,
                TYPE_RGB_FLT, int(intent), flags,
            )
            if not self._transform:
                raise ICCProfileError(self._detail("The ICC profile cannot form an RGB-to-sRGB transform"))
            version = lib.cmsGetEncodedCMMversion()
            self.metadata = {
                "engine": "LittleCMS 2", "library": self._library.path,
                "engine_version": f"{version // 1000}.{version % 1000 // 10}.{version % 10}",
                "input_profile_sha256": hashlib.sha256(profile[:declared]).hexdigest(),
                "input_space": "ICC RGB", "output_space": "encoded sRGB",
                "sample_format": "RGB float32; no 8-bit intermediate",
                "rendering_intent": int(intent),
                "black_point_compensation": bool(black_point_compensation),
                "output_clipping": "none; caller handles destination gamut",
            }
        except Exception:
            self.close()
            raise

    def _record_error(self, _context, code, message):
        self._error = f"LittleCMS {code}: {(message or b'').decode('utf-8', errors='replace')}"

    def _detail(self, message):
        return f"{message}: {self._error}" if self._error else message

    def apply(self, array):
        """Convert one normalized encoded RGB HWC strip into float32 sRGB."""
        with self._lock:
            if not self._transform:
                raise ICCTransformError("ICC transform has been closed")
            values = np.asarray(array)
            if values.ndim != 3 or values.shape[2] != 3:
                raise ValueError("ICC input must be an HWC RGB array with exactly 3 channels")
            if not np.issubdtype(values.dtype, np.floating):
                raise TypeError("ICC input must contain normalized floating-point RGB samples")
            if not np.isfinite(values).all():
                raise ValueError("ICC input contains non-finite RGB samples")
            if values.size and (np.min(values) < 0 or np.max(values) > 1):
                raise ValueError("ICC input must be normalized encoded SDR RGB in [0,1]")
            source = np.ascontiguousarray(values, dtype=np.float32)
            result = np.empty(source.shape, dtype=np.float32)
            self._error = None
            count = source.shape[0] * source.shape[1]
            # The API count is uint32; bounded calls avoid overflow even if a
            # caller supplies a large frame rather than the intended strip.
            for offset in range(0, count, 1048576):
                size = min(1048576, count - offset)
                self._library.lib.cmsDoTransform(
                    self._transform, source.ctypes.data + offset * 12,
                    result.ctypes.data + offset * 12, size,
                )
                if self._error:
                    raise ICCTransformError(self._detail("ICC pixel conversion failed"))
            if not np.isfinite(result).all():
                raise ICCTransformError("ICC conversion produced non-finite RGB samples")
            return result

    def close(self):
        with self._lock:
            if self._library is not None:
                lib = self._library.lib
                if self._transform:
                    lib.cmsDeleteTransform(self._transform)
                    self._transform = None
                for name in ("_target", "_source"):
                    handle = getattr(self, name)
                    if handle:
                        lib.cmsCloseProfile(handle)
                        setattr(self, name, None)
                if self._context:
                    lib.cmsDeleteContext(self._context)
                    self._context = None
            self._buffer = self._callback = None

    def __enter__(self):
        if not self._transform:
            raise ICCTransformError("ICC transform has been closed")
        return self

    def __exit__(self, *_args):
        self.close()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass
