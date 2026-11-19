"""Five true SpektraLab renders for conditional, high-bit-depth PSD layers.

The stock host owns all photography calculations. It receives a tagged 16-bit
sRGB TIFF and exports tagged sRGB TIFF16 files; no intermediate 8-bit image is
used. The host does not expose its scanner unsharp mask or a grain-seed field,
so this adapter reports those limits rather than pretending to control them.
"""
from __future__ import annotations

import math
import numbers
import os
from pathlib import Path
import tempfile
import time

from lab_backend import BackendError, _Host, _parameters


_KEYS = {"film", "paper", "exposure_ev", "print_exposure", "grain", "halation",
         "dir_couplers", "auto_exposure", "seed"}
_STAGES = (
    ("base", "胶片与相纸基础"),
    ("exposure", "曝光与印相"),
    ("halation", "散射与光晕"),
    ("dir", "DIR 显影扩散"),
    ("grain", "胶片颗粒"),
)
_FIXED = {
    "glare_active": False,
    "scanner_lens_blur": 0.0,
    "lens_blur_um": 0.0,
    "contrast_mask_active": False,
    "halation_boost_ev": 0.0,
    "halation_headroom_ev": 0.0,
    "exposure_baseline_ev": 0.0,
    "extended_dynamic_range": False,
    "scan_film": False,
}
_UNEXPOSED = {
    "scanner.unsharp_mask": {
        "control": "not exposed by the host wire schema",
        "native_default": [0.7, 0.7],
        "treatment": "retained at the native default; identical setting across stages",
    },
    "camera.diffusion_filter.active": {
        "control": "not exposed by the host wire schema", "native_default": False,
    },
    "enlarger.diffusion_filter.active": {
        "control": "not exposed by the host wire schema", "native_default": False,
    },
    "enlarger.lens_blur": {
        "control": "not exposed by the host wire schema", "native_default": 0.0,
    },
}


def _rows(shape):
    height, width = shape[:2]
    count = max(1, 524_288 // (width * 3))
    for start in range(0, height, count):
        yield slice(start, min(height, start + count))


def _frame(np, shape, runtime, role):
    directory = runtime.get("work_dir") or runtime.get("stage_dir")
    if not directory:
        return np.empty(shape, dtype=np.float32)
    directory = Path(directory).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix=f"native-{role}-", suffix=".npy", dir=directory, delete=False) as handle:
        path = Path(handle.name)
    array = np.lib.format.open_memmap(path, mode="w+", dtype=np.float32, shape=shape)
    registry = runtime.get("_memmaps")
    if isinstance(registry, list):
        registry.append(array)
    return array


def _validate(linear_srgb, settings, runtime):
    import numpy as np

    if not isinstance(settings, dict):
        raise TypeError("SpektraLab PSD settings must be a dictionary")
    unknown = set(settings) - _KEYS
    if unknown:
        raise ValueError("Unknown SpektraLab PSD settings: " + ", ".join(sorted(unknown)))
    # Reuse the adapter's documented numeric bounds and boolean validation.
    _parameters(settings)
    seed = settings.get("seed", 20261112)
    if isinstance(seed, bool) or not isinstance(seed, numbers.Integral) or not 0 <= seed <= 4294967295:
        raise ValueError("seed must be an integer from 0 to 4294967295")
    if not isinstance(runtime, dict):
        raise TypeError("runtime must be a dictionary")
    if os.name != "nt":
        raise RuntimeError("This SpektraLab host adapter requires Windows")
    for key in ("lab_host", "lab_resources"):
        if not isinstance(runtime.get(key), (str, Path)):
            raise ValueError(f"runtime.{key} must be a local path")
    executable = Path(runtime["lab_host"]).expanduser().resolve()
    resources = Path(runtime["lab_resources"]).expanduser().resolve()
    if not executable.is_file() or not resources.is_dir():
        raise FileNotFoundError("SpektraLab host or engine resources are missing")
    timeout = float(runtime.get("timeout_s", 300))
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("runtime.timeout_s must be a finite positive number")
    if not isinstance(linear_srgb, np.ndarray) or linear_srgb.ndim != 3 or linear_srgb.shape[2] != 3:
        raise TypeError("linear_srgb must be an HWC NumPy RGB array")
    if not np.issubdtype(linear_srgb.dtype, np.floating):
        raise TypeError("linear_srgb must contain floating-point linear RGB values")
    if linear_srgb.shape[0] < 1 or linear_srgb.shape[1] < 1:
        raise ValueError("linear_srgb must not be empty")
    for rows in _rows(linear_srgb.shape):
        part = linear_srgb[rows]
        if not np.isfinite(part).all() or np.any(part < 0) or np.any(part > 1):
            raise ValueError("SpektraLab PSD input must be finite SDR linear sRGB in [0,1]")
    return executable, resources, timeout, int(seed)


def _stage_parameters(settings, index):
    current = dict(settings)
    current.pop("seed", None)
    if index == 0:
        current.update(auto_exposure=False, exposure_ev=0.0, print_exposure=1.0)
    current["halation"] = settings.get("halation", 0.25) if index >= 2 else 0.0
    current["dir_couplers"] = settings.get("dir_couplers", 1.0) if index >= 3 else 0.0
    current["grain"] = settings.get("grain", 0.3) if index >= 4 else 0.0
    return {**_parameters(current), **_FIXED}


def _read_tiff16(path, expected_shape, runtime, role):
    import numpy as np
    import tifffile

    with tifffile.TiffFile(path) as document:
        page = document.pages[0]
        bits = page.bitspersample
        if isinstance(bits, tuple):
            bits = max(bits)
        if bits != 16 or page.dtype != np.dtype("uint16"):
            raise RuntimeError("Host export was not an unsigned 16-bit TIFF")
        icc = page.tags.get(34675)
        if icc is None or not icc.value:
            raise RuntimeError("Host TIFF16 export is missing its sRGB ICC profile")
        profile_bytes = len(icc.value)
        if page.shape != tuple(expected_shape):
            raise RuntimeError(f"Host TIFF16 shape mismatch: {page.shape}, expected {expected_shape}")
    # Avoid page.asarray() of the full frame. Raw TIFFs map directly; compressed
    # host exports are decoded with bounded scanline/tile-row reads.
    rgb = _frame(np, expected_shape, runtime, role)
    pixels = opened = None
    decoding = "tifffile memory map"
    minimum, maximum, zeros, full = 65535, 0, 0, 0
    try:
        try:
            pixels = tifffile.memmap(path, mode="r")
        except (ValueError, NotImplementedError):
            import OpenImageIO as oiio
            opened = oiio.ImageInput.open(str(path))
            if opened is None:
                raise RuntimeError("OpenImageIO could not open host TIFF16")
            decoding = "OpenImageIO bounded strip decoding"
        height, width = expected_shape[:2]
        if opened is not None and opened.spec().tile_width:
            decoding = "OpenImageIO bounded tile-row decoding"
            row_count = max(1, int(opened.spec().tile_height))
            slices = (slice(y, min(height, y + row_count)) for y in range(0, height, row_count))
        else:
            slices = _rows(expected_shape)
        for rows in slices:
            if pixels is not None:
                block = pixels[rows]
            elif opened.spec().tile_width:
                block = np.asarray(opened.read_tiles(0, width, rows.start, rows.stop, 0, 1, 0, 3, oiio.UINT16))
            else:
                block = np.asarray(opened.read_scanlines(rows.start, rows.stop, 0, 0, 3, oiio.UINT16))
            if block.dtype != np.uint16 or block.shape != (rows.stop - rows.start, width, 3):
                raise RuntimeError(f"Host TIFF16 strip has invalid dtype/shape: {block.dtype}, {block.shape}")
            minimum, maximum = min(minimum, int(block.min())), max(maximum, int(block.max()))
            zeros += int(np.count_nonzero(block == 0)); full += int(np.count_nonzero(block == 65535))
            rgb[rows] = block.astype(np.float32) / np.float32(65535)
        if isinstance(rgb, np.memmap):
            rgb.flush()
    finally:
        if opened is not None:
            opened.close()
        if isinstance(pixels, np.memmap):
            pixels._mmap.close()
    samples = int(expected_shape[0]) * int(expected_shape[1]) * 3
    return rgb, {
        "storage_precision": "uint16 TIFF", "icc_profile_bytes": profile_bytes,
        "sample_min": minimum, "sample_max": maximum,
        "zero_channel_fraction": zeros / samples,
        "full_scale_channel_fraction": full / samples,
        "decoder": decoding, "file_backed_float_output": isinstance(rgb, np.memmap),
        "pre_export_clip_fraction": "not exposed by native host",
    }


def render_passes(linear_srgb, settings, runtime):
    """Return five final-colour renders from one native session, or raise.

    Stage values are encoded sRGB floats decoded from native TIFF16 output.
    No final artifacts are committed here. All private TIFFs and the host
    process are cleaned up on success or failure. Explicit Lab selection is
    never silently redirected to a different engine by this module.
    """
    import numpy as np
    import tifffile
    from PIL import ImageCms

    executable, resources, timeout, seed = _validate(linear_srgb, settings, runtime)
    started = time.perf_counter()
    warnings = [
        "SpektraLab PSD uses tagged TIFF16 input/output; it does not provide float-image readback through this host.",
        "The native host does not expose a grain-seed parameter. The requested seed is recorded but does not control native grain; the native generator is retained.",
        "Native scanner unsharp_mask is not exposed by the host and remains at its default [0.7,0.7]. This backend is not claimed pixel-identical to the Python PSD mode.",
    ]
    icc = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    metadata = {
        "engine": "spektralab", "mode": "native_conditional_psd_tiff16",
        "stage_order": [key for key, _ in _STAGES],
        "input_shape": list(linear_srgb.shape),
        "input_transport": {"encoding": "sRGB", "precision": "uint16 TIFF", "writer": "bounded strips into uncompressed TIFF memory map"},
        "output_transport": "encoded sRGB uint16 TIFF converted to float32 [0,1]",
        "requested_seed": seed,
        "seed_control": {"supported": False, "actual": "native generator; no seed field in host protocol"},
        "fixed_host_parameters": dict(_FIXED), "unexposed_native_controls": _UNEXPOSED,
        "auto_exposure": {
            "policy": "base off; later stages use the same unchanged input and requested native auto-exposure setting",
            "manual_ev": "kept in exposure_compensation_ev; never combined with auto EV",
            "measured_ev": "not returned by this host interface",
        },
        "stage_records": [],
    }
    stages = []
    host = None
    session = None
    private_parent = runtime.get("work_dir") or runtime.get("stage_dir")
    if private_parent:
        private_parent = Path(private_parent).expanduser().resolve()
        private_parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="spektra-lab-psd-", dir=private_parent) as folder:
        work = Path(folder)
        source = work / "input-srgb16.tif"
        # The native renderer still evaluates a full frame. Input transport and
        # retained output frames no longer require several full-frame copies.
        transport = tifffile.memmap(source, shape=linear_srgb.shape, dtype=np.uint16,
            photometric="rgb", metadata=None, bigtiff=int(linear_srgb.size) * 2 > 3_900_000_000,
            extratags=[(34675, "B", len(icc), icc, False)])
        error_maximum, error_sum = 0.0, 0.0
        try:
            for rows in _rows(linear_srgb.shape):
                values = np.asarray(linear_srgb[rows], dtype=np.float64)
                encoded = np.where(values <= 0.0031308, values * 12.92,
                                   1.055 * np.power(values, 1 / 2.4) - 0.055)
                quantized = np.rint(np.clip(encoded, 0, 1) * 65535).astype(np.uint16)
                difference = np.abs(encoded - quantized.astype(np.float64) / 65535)
                error_maximum = max(error_maximum, float(difference.max()))
                error_sum += float(difference.sum())
                transport[rows] = quantized
            transport.flush()
        finally:
            transport._mmap.close()
        metadata["input_transport"].update(
            max_abs_quantization_error_encoded=error_maximum,
            mean_abs_quantization_error_encoded=error_sum / int(linear_srgb.size))
        try:
            host = _Host(executable, resources, time.monotonic() + timeout)
            hello = host.call("hello")
            metadata["host_version"] = hello.get("host_version")
            metadata["host_backend"] = hello.get("backend")
            if hello.get("protocol") != 1:
                raise BackendError("protocol_error", "SpektraLab protocol 1 is required", "hello")
            backend = hello.get("backend", {})
            if not backend.get("available"):
                raise BackendError("backend_unavailable", backend.get("error", "Native GPU unavailable"), "hello")
            initial = _stage_parameters(settings, 0)
            opened = host.call("open", {"path": str(source), "params": initial})
            session = opened["session"]
            if [opened["height"], opened["width"], 3] != list(linear_srgb.shape):
                raise RuntimeError("Native input decode changed the processed dimensions")
            metadata["open_timings_ms"] = opened.get("timings_ms", {})
            for index, (key, name) in enumerate(_STAGES):
                pass_started = time.perf_counter()
                requested = _stage_parameters(settings, index)
                if index:
                    resolved = host.call("set_params", {"session": session, "delta": requested})["params"]
                else:
                    resolved = host.call("get_params", {"session": session})["params"]
                target = work / (key + ".tif")
                exported = host.call("export_image", {"session": session, "path": str(target),
                                                      "format": "tiff16", "color_space": "sRGB",
                                                      "long_edge": 0, "overwrite": False})
                rgb, diagnostics = _read_tiff16(target, linear_srgb.shape, runtime, key)
                info = {"requested_host_parameters": requested, "resolved_host_parameters": resolved,
                        "precision": diagnostics, "elapsed_s": time.perf_counter() - pass_started,
                        "export": {field: exported[field] for field in ("width", "height", "bytes", "color_space")
                                   if field in exported}}
                stages.append({"key": key, "name": name, "rgb": rgb, "metadata": info})
                metadata["stage_records"].append({"key": key, **info})
            host.call("close", {"session": session})
            session = None
        finally:
            if host is not None:
                # Shutdown also releases a session when an earlier method
                # failed. The process is killed after the adapter's deadline.
                metadata["host_log"] = host.close()
                metadata["last_api_timings_s"] = dict(host.timings)
    metadata["elapsed_s"] = time.perf_counter() - started
    metadata["peak_frame_count_retained"] = len(stages)
    metadata["retained_stage_storage"] = "disk .npy memmaps" if private_parent else "RAM arrays (caller provided no work_dir)"
    metadata["native_processing"] = "whole-frame native renderer; actual GPU/RAM limits are reported by host, not a fixed adapter pixel cap"
    return {"stages": stages, "metadata": metadata, "warnings": warnings}
