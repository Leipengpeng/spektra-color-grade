"""Write genuine 16-bit RGB PSD/PSB conditional stage differences.

The file has no final-image cover layer.  Its visible stack is the retained
source, an opaque base, and Linear Light conditional-difference layers.  For
default layer opacity a, S_i = ((Y_i - Y_(i-1))/a + 1) / 2.  Opacity is selected
with enough encoding and backdrop headroom that Photoshop's clip-before-
opacity blending reconstructs the selected path, up to 16-bit quantization.
Small effects usually start near 50% so the user can increase or decrease them.
These are *conditional* differences, not independent
physical effects: editing the base, reordering layers, or hiding intermediate
layers does not rerun the original image-processing engine.

Only NumPy and the standard library are required.  PSD version 1 or PSB version
2, uncompressed big-endian 16-bit channels, the global Lr16 block, Unicode names
(luni), ICC and a genuine merged composite are written explicitly.  File data
is streamed in row stripes, so input arrays can be disk-backed NumPy memmaps.
The Adobe specification is
at https://www.adobe.com/devnet-apps/photoshop/fileformatashtml/ .  In particular,
the documented Linear Light key is b'lLit'.

The mathematical error bounds returned here apply to normalized 0..65535 file
samples and encoded-sRGB blending.  Photoshop's native compositor, its internal
16-bpc arithmetic and any different blend-gamma settings need a separate host
check; this writer does not claim bit-exact Photoshop reconstruction.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import struct
import tempfile
from typing import BinaryIO
import xml.etree.ElementTree as ET

import numpy as np

__all__ = ["write_layered_psd", "choose_document_format", "estimate_document_bytes"]

_Q = 65535
_MAX_PSD_BYTES = 2_000_000_000
_MAX_PSD_DIMENSION = 30_000
_MAX_PSB_DIMENSION = 300_000
_CHUNK_SAMPLES = 262_144
_NOTICE = (
    "Conditional stage differences for this input, parameters and order. "
    "Hiding, reordering or editing layers does not rerun the engine. "
    "Effect opacity is adaptive; the default reconstructs this rendering and "
    "100% provides only the documented per-layer enhancement headroom. "
    "Use encoded-sRGB blending; Photoshop host arithmetic is not bit-exact verified."
)


def _u32(value: int) -> bytes:
    return struct.pack(">I", value)


def _length(value: int, version: int) -> bytes:
    return struct.pack(">Q" if version == 2 else ">I", value)


def _geometry(width: int, height: int, stage_count: int) -> None:
    for name, value in (("width", width), ("height", height), ("stage_count", stage_count)):
        if isinstance(value, (bool, np.bool_)) or not isinstance(value, (int, np.integer)):
            raise TypeError(f"{name} must be an integer")
    if not (1 <= width <= _MAX_PSB_DIMENSION and 1 <= height <= _MAX_PSB_DIMENSION):
        raise ValueError("PSD/PSB dimensions must be between 1 and 300,000 pixels")
    if not 0 <= stage_count <= 32764:
        raise ValueError("stage_count must be between 0 and 32764")


def estimate_document_bytes(width: int, height: int, stage_count: int, metadata_bytes: int = 0) -> int:
    """Conservative raw-document estimate for format selection and disk planning.

    ``stage_count`` is the number of conditional difference layers, not the
    number of full stage images; three additional layers hold original, base
    and retouch data.  One MiB is reserved for names/resources in addition to
    ``metadata_bytes``.  The writer computes the exact size before writing and
    promotes to PSB even if an unusually large manifest exceeds this estimate.
    """
    _geometry(width, height, stage_count)
    if isinstance(metadata_bytes, (bool, np.bool_)) or not isinstance(metadata_bytes, (int, np.integer)) or metadata_bytes < 0:
        raise ValueError("metadata_bytes must be a nonnegative integer")
    layer_count = int(stage_count) + 3
    return int(width) * int(height) * (layer_count * 8 + 6) + layer_count * 2048 + 1_048_576 + int(metadata_bytes)


def choose_document_format(width: int, height: int, stage_count: int, metadata_bytes: int = 0) -> str:
    """Choose 'psd' or 'psb' without allocating or reading pixel arrays.

    PSD is restricted to 30,000 pixels per side and a 2 GB file.  PSB supports
    300,000 pixels per side, subject to filesystem, storage and application
    limits; neither this helper nor PSB promises unbounded image dimensions.
    """
    estimate = estimate_document_bytes(width, height, stage_count, metadata_bytes)
    return "psb" if max(width, height) > _MAX_PSD_DIMENSION or estimate > _MAX_PSD_BYTES else "psd"


def _unicode(value: str) -> bytes:
    data = value.encode("utf-16-be")
    return _u32(len(data) // 2) + data


def _tag(key: bytes, data: bytes, alignment: int = 2) -> bytes:
    return b"8BIM" + key + _u32(len(data)) + data + b"\0" * (-len(data) % alignment)


def _resource(resource_id: int, data: bytes) -> bytes:
    # Empty Pascal resource name occupies two bytes (even padding).
    return (b"8BIM" + struct.pack(">H", resource_id) + b"\0\0" +
            _u32(len(data)) + data + b"\0" * (len(data) % 2))


def _chunks(height: int, width: int):
    rows = max(1, _CHUNK_SAMPLES // width)
    for start in range(0, height, rows):
        yield slice(start, min(start + rows, height))


def _validate_rgb(value: np.ndarray, label: str, shape: tuple | None = None) -> None:
    if not isinstance(value, np.ndarray) or value.ndim != 3 or value.shape[2] != 3:
        raise ValueError(f"{label} must be an HWC NumPy array with three RGB channels")
    if not np.issubdtype(value.dtype, np.floating):
        raise TypeError(f"{label} must contain normalized floating-point RGB, not integers")
    if shape is not None and value.shape != shape:
        raise ValueError(f"{label} shape {value.shape} differs from {shape}")
    height, width = value.shape[:2]
    if not (1 <= height <= _MAX_PSB_DIMENSION and 1 <= width <= _MAX_PSB_DIMENSION):
        raise ValueError("PSD/PSB dimensions must be between 1 and 300,000 pixels")
    for rows in _chunks(height, width):
        part = value[rows]
        if not np.isfinite(part).all() or np.any(part < 0) or np.any(part > 1):
            raise ValueError(f"{label} contains nonfinite values or RGB outside [0, 1]")


def _quantize(values: np.ndarray) -> np.ndarray:
    # Always compute in float64; casting float32 after multiplying by 65535 can
    # otherwise introduce an extra rounding error at half-integer boundaries.
    scaled = np.asarray(values, dtype=np.float64) * _Q
    np.rint(scaled, out=scaled)
    return scaled.astype(">u2")


def _plane(layer: dict, channel: int, rows: slice, width: int) -> np.ndarray:
    if channel == -1:
        value = 0 if layer["kind"] == "retouch" else _Q
        return np.full((rows.stop - rows.start, width), value, dtype=">u2")
    if layer["kind"] == "retouch":
        return np.zeros((rows.stop - rows.start, width), dtype=">u2")
    if layer["kind"] == "delta":
        current = np.asarray(layer["rgb"][rows, :, channel], dtype=np.float64)
        previous = np.asarray(layer["previous"][rows, :, channel], dtype=np.float64)
        return _quantize(((current - previous) * layer.get("encoded_gain", 1.0) + 1.0) * 0.5)
    return _quantize(layer["rgb"][rows, :, channel])


def _layer_record(layer: dict, height: int, width: int, index: int, version: int = 1) -> bytes:
    name = layer["name"]
    # ASCII fallback is deliberately separate from the authoritative luni name.
    fallback = name.encode("macroman", errors="replace")[:255]
    pascal = bytes((len(fallback),)) + fallback
    pascal += b"\0" * (-len(pascal) % 4)
    extra = (_u32(0) + _u32(0) + pascal +
             _tag(b"luni", _unicode(name)) + _tag(b"lyid", _u32(index + 1)))
    channel_bytes = 2 + height * width * 2  # compression word plus raw u16 plane
    channels = b"".join(struct.pack(">hQ" if version == 2 else ">hI", c, channel_bytes) for c in (0, 1, 2, -1))
    # Spec's visibility bit is inverted in actual PSD files: 0 = visible.
    # bit 3 is the Photoshop 5+ validity flag; bit 4 stays clear (real pixel data).
    flags = 8 | (0 if layer["visible"] else 2)
    return (struct.pack(">4iH", 0, 0, height, width, 4) + channels + b"8BIM" +
            layer["blend"] + bytes((layer.get("opacity", 255), 0, flags, 0)) + _u32(len(extra)) + extra)


def _difference_encoding(previous: np.ndarray, current: np.ndarray) -> dict:
    """Choose boostable opacity without clipping the default reconstruction.

    Linear Light clips the full-strength blend *before* layer opacity.  Merely
    ensuring S is in [0,1] is insufficient.  We additionally require
    B + delta/a to be in [0,1], i.e. a >= delta/(1-B) for positive deltas and
    a >= -delta/B for negative deltas.  Saturating transitions can therefore
    force a=1; that limitation is recorded instead of promising a false boost.
    """
    height, width = previous.shape[:2]
    magnitude = headroom = 0.0
    for rows in _chunks(height, width):
        before = np.asarray(previous[rows], dtype=np.float64)
        delta = np.asarray(current[rows], dtype=np.float64) - before
        magnitude = max(magnitude, float(np.max(np.abs(delta))))
        ratios = np.zeros_like(delta)
        np.divide(delta, 1.0-before, out=ratios, where=delta > 0)
        headroom = max(headroom, float(np.max(ratios)))
        ratios.fill(0)
        np.divide(-delta, before, out=ratios, where=delta < 0)
        headroom = max(headroom, float(np.max(ratios)))
    minimum = min(1.0, max(0.5, magnitude, headroom))
    opacity = min(255, max(1, int(np.ceil(minimum * 255))))
    return {"opacity": opacity, "encoded_gain": 255.0/opacity,
            "minimum_opacity_from_delta_range": magnitude,
            "minimum_opacity_from_backdrop_headroom": headroom}


def _measure(base: np.ndarray, delta_layers: list[dict], final: np.ndarray) -> dict:
    maximum_error = maximum_vs_merged = merged_error = 0.0
    height, width = base.shape[:2]
    for channel in range(3):
        for rows in _chunks(height, width):
            current = _quantize(base[rows, :, channel]).astype(np.float64) / _Q
            for layer in delta_layers:
                encoded = _plane(layer, channel, rows, width).astype(np.float64) / _Q
                opacity = layer["opacity"]/255.0
                blended = np.clip(current + 2.0 * encoded - 1.0, 0.0, 1.0)
                current = (1.0-opacity)*current + opacity*blended
            target = np.asarray(final[rows, :, channel], dtype=np.float64)
            merged = _quantize(target).astype(np.float64) / _Q
            maximum_error = max(maximum_error, float(np.max(np.abs(current - target))))
            maximum_vs_merged = max(maximum_vs_merged, float(np.max(np.abs(current - merged))))
            merged_error = max(merged_error, float(np.max(np.abs(merged - target))))
    return {
        "max_abs_layer_reconstruction_error": maximum_error,
        "max_abs_layer_vs_merged_error": maximum_vs_merged,
        "max_abs_merged_quantization_error": merged_error,
        "layer_reconstruction_error_bound": (sum(l["opacity"]/255 for l in delta_layers) + 0.5) / _Q,
        "layer_vs_merged_error_bound": (sum(l["opacity"]/255 for l in delta_layers) + 1.0) / _Q,
        "merged_quantization_error_bound": 0.5 / _Q,
        "bound_scope": "normalized 16-bit file values; encoded-sRGB Linear Light; exact real arithmetic plus float64 roundoff",
    }


def _xmp(metadata: dict | None, layer_summaries: list[dict], metrics: dict) -> bytes:
    # Embed provenance as a private XMP property, not as executable Adobe settings.
    ns = {
        "x": "adobe:ns:meta/",
        "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
        "spektra": "https://spektra.local/ns/psd/1.0/",
    }
    for prefix, uri in ns.items():
        ET.register_namespace(prefix, uri)
    root = ET.Element(f"{{{ns['x']}}}xmpmeta")
    rdf = ET.SubElement(root, f"{{{ns['rdf']}}}RDF")
    description = ET.SubElement(rdf, f"{{{ns['rdf']}}}Description", {f"{{{ns['rdf']}}}about": ""})
    payload = {"format_version": 2, "working_space": "encoded sRGB", "notice": _NOTICE,
               "layers_bottom_to_top": layer_summaries, "quantization": metrics,
               "metadata": metadata}
    ET.SubElement(description, f"{{{ns['spektra']}}}Manifest").text = json.dumps(
        payload, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    return ET.tostring(root, encoding="utf-8", xml_declaration=False)


def _write_channel(file: BinaryIO, layer: dict, channel: int, height: int, width: int) -> None:
    file.write(b"\0\0")  # raw compression
    for rows in _chunks(height, width):
        file.write(_plane(layer, channel, rows, width).tobytes(order="C"))


def write_layered_psd(
    output_path: Path,
    original_rgb: np.ndarray,
    base_rgb: np.ndarray,
    stages: list[dict],
    icc_profile: bytes,
    metadata: dict | None = None,
    *,
    document_format: str = "auto",
) -> dict:
    """Atomically save a 16-bit RGB PSD/PSB and return a JSON-compatible report.

    Arrays must be floating-point HWC RGB in [0,1], already encoded in sRGB,
    with identical shapes.  Each stage is ``{'name': str, 'rgb': ndarray}`` and
    contains the *full output after that stage*, not a difference.  ``icc_profile``
    must be the matching sRGB RGB ICC profile; it is embedded byte-for-byte.
    Metadata must be a JSON-serializable dictionary.  Inputs are never modified.

    Stack, bottom to top: visible retained original, opaque base, conditional
    Linear Light differences at adaptive default opacity, and a fully
    transparent ordinary retouch layer.  The base's default opacity is 100%;
    reducing it reveals the retained source underneath.
    The merged composite is the actual final array quantized independently.
    Existing output files are atomically replaced only after a complete write.
    ``document_format`` is auto/psd/psb.  In auto mode, a .psb suffix forces PSB;
    a .psd request is promoted to .psb when PSD's geometry or size is exceeded.
    Always use the returned path, which reflects any suffix promotion.  An
    explicit psd request that is too large raises rather than silently resizing.
    Arrays and memmaps are scanned and written in stripes, never copied whole.
    """
    output_path = Path(output_path)
    if output_path.suffix.lower() not in (".psd", ".psb"):
        raise ValueError("output_path must have a .psd or .psb extension")
    if document_format not in ("auto", "psd", "psb"):
        raise ValueError("document_format must be auto, psd or psb")
    if document_format == "psd" and output_path.suffix.lower() == ".psb":
        raise ValueError("A .psb filename conflicts with document_format='psd'")
    _validate_rgb(original_rgb, "original_rgb")
    _validate_rgb(base_rgb, "base_rgb", original_rgb.shape)
    if not isinstance(stages, list):
        raise TypeError("stages must be a list of {'name': str, 'rgb': ndarray}")
    if len(stages) + 3 > 32767:
        raise ValueError("Too many layers for PSD's signed 16-bit layer count")
    for index, stage in enumerate(stages):
        if not isinstance(stage, dict) or not isinstance(stage.get("name"), str) or not stage["name"].strip():
            raise ValueError(f"stages[{index}] must have a nonempty string name")
        if "\0" in stage["name"]:
            raise ValueError("Layer names cannot contain NUL characters")
        _validate_rgb(stage.get("rgb"), f"stages[{index}].rgb", original_rgb.shape)
    if not isinstance(icc_profile, bytes):
        raise TypeError("icc_profile must be bytes")
    if (len(icc_profile) < 128 or icc_profile[36:40] != b"acsp" or
            icc_profile[16:20] != b"RGB " or int.from_bytes(icc_profile[:4], "big") != len(icc_profile)):
        raise ValueError("icc_profile must contain a complete RGB ICC profile")
    if metadata is not None and not isinstance(metadata, dict):
        raise TypeError("metadata must be a dict or None")

    height, width = original_rgb.shape[:2]
    layers = [
        {"name": "原片底图（保留）", "kind": "original", "rgb": original_rgb, "blend": b"norm", "visible": True, "opacity": 255},
        {"name": "基础调色", "kind": "base", "rgb": base_rgb, "blend": b"norm", "visible": True, "opacity": 255},
    ]
    previous = base_rgb
    for stage in stages:
        layers.append({"name": stage["name"], "kind": "delta", "rgb": stage["rgb"],
                       "previous": previous, "blend": b"lLit", "visible": True,
                       **_difference_encoding(previous, stage["rgb"])})
        previous = stage["rgb"]
    layers.append({"name": "后续修饰", "kind": "retouch", "blend": b"norm", "visible": True, "opacity": 255})
    final = stages[-1]["rgb"] if stages else base_rgb
    summaries = [{"name": layer["name"], "kind": layer["kind"],
                  "blend_mode_key": layer["blend"].decode("ascii"),
                  "visible": layer["visible"], "opacity": layer["opacity"],
                  "default_opacity": layer["opacity"],
                  "default_opacity_percent": 100*layer["opacity"]/255,
                  "boost_at_100_percent": layer.get("encoded_gain", 1.0),
                  "encoded_gain": layer.get("encoded_gain", 1.0),
                  "headroom_limited": layer.get("opacity", 255) > 128 if layer["kind"] == "delta" else False,
                  "minimum_opacity_from_delta_range": layer.get("minimum_opacity_from_delta_range"),
                  "minimum_opacity_from_backdrop_headroom": layer.get("minimum_opacity_from_backdrop_headroom"),
                  "alpha": 0 if layer["kind"] == "retouch" else 65535,
                  "depth": 16} for layer in layers]
    metrics = _measure(base_rgb, layers[2:-1], final)
    xmp = _xmp(metadata, summaries, metrics)
    version_info = (_u32(1) + b"\x01" + _unicode("Spektra 16-bit PSD/PSB writer") +
                    _unicode("Adobe Photoshop") + _u32(1))
    resources = (_resource(1039, icc_profile) + _resource(1057, version_info) +
                 _resource(1060, xmp))
    def layout(version):
        records = b"".join(_layer_record(layer, height, width, i, version) for i, layer in enumerate(layers))
        payload_length = 2 + len(records) + len(layers) * 4 * (2 + height * width * 2)
        padding = -payload_length % 4
        length_size = 8 if version == 2 else 4
        # Empty standard LayerInfo, global-mask length, global Lr16 tag header.
        mask_length = length_size + 4 + 8 + length_size + payload_length + padding
        size = 26 + 4 + 4 + len(resources) + length_size + mask_length + 2 + height * width * 6
        return records, payload_length, padding, mask_length, size

    selected = document_format
    reason = "explicit format"
    if selected == "auto":
        selected = "psb" if output_path.suffix.lower() == ".psb" else choose_document_format(width, height, len(stages), len(resources))
        reason = "explicit .psb suffix" if output_path.suffix.lower() == ".psb" else "geometry and uncompressed file-size planning"
    version = 2 if selected == "psb" else 1
    if version == 1 and max(width, height) > _MAX_PSD_DIMENSION:
        raise ValueError("Explicit PSD is limited to 30,000 pixels per side; select PSB or auto")
    records, layer_data_length, layer_block_padding, layer_mask_length, expected_size = layout(version)
    if version == 1 and expected_size > _MAX_PSD_BYTES:
        if document_format == "psd":
            raise ValueError("Explicit PSD would exceed 2 GB; select PSB or auto")
        version, selected, reason = 2, "psb", "exact document size exceeds PSD's 2 GB limit"
        records, layer_data_length, layer_block_padding, layer_mask_length, expected_size = layout(version)
    output_path = output_path.with_suffix("." + selected)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", prefix=f".{output_path.stem}.",
                                         suffix=".tmp", dir=output_path.parent, delete=False) as file:
            temporary = Path(file.name)
            file.write(struct.pack(">4sH6sHIIHH", b"8BPS", version, b"\0" * 6, 3, height, width, 16, 3))
            file.write(_u32(0))  # RGB has no color-mode data.
            file.write(_u32(len(resources)))
            file.write(resources)
            file.write(_length(layer_mask_length, version))
            file.write(_length(0, version))  # Empty standard LayerInfo in deep PSD/PSB.
            file.write(_u32(0))  # No global layer mask.
            # The PSB special key Lr16 has a uint64 length even with 8BIM.
            file.write(b"8BIMLr16" + _length(layer_data_length, version))
            file.write(struct.pack(">h", len(layers)))
            file.write(records)  # PSD records and API layer order are bottom-to-top.
            for layer in layers:
                for channel in (0, 1, 2, -1):
                    _write_channel(file, layer, channel, height, width)
            file.write(b"\0" * layer_block_padding)
            file.write(b"\0\0")  # Genuine final merged RGB, raw compression.
            final_layer = {"kind": "merged", "rgb": final}
            for channel in range(3):
                for rows in _chunks(height, width):
                    file.write(_plane(final_layer, channel, rows, width).tobytes(order="C"))
            if file.tell() != expected_size:
                raise RuntimeError(f"Internal PSD size mismatch: {file.tell()} != {expected_size}")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, output_path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)

    return {
        "path": str(output_path.resolve()), "format": selected.upper(), "version": version,
        "format_selection": reason,
        "width": width, "height": height, "depth": 16, "channels": 3,
        "working_space": "encoded sRGB", "icc_sha256": hashlib.sha256(icc_profile).hexdigest(),
        "file_bytes": expected_size, "layer_count": len(layers),
        "stage_count": len(stages), "layers_bottom_to_top": summaries,
        "merged_composite": "actual final stage, independently quantized to 16-bit",
        "difference_encoding": "For opacity a, S = ((Y_i-Y_(i-1))/a+1)/2; output = (1-a)*B + a*clip(B+2*S-1,0,1). a respects encoding and backdrop headroom.",
        "quantization": metrics, "notice": _NOTICE,
        "photoshop_host_verified": False,
    }
