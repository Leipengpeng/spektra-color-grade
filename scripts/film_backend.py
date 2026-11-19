"""Local spektrafilm adapter for an opaque, display-encoded sRGB proxy.

The outer CLI prepares the proxy and selects the Python environment. This
module has no CLI, network or VLM dependency, and imports the engine lazily.
Only the render-time parameter dataclasses are changed; stock profiles and
upstream files are never edited.
"""
from __future__ import annotations

import contextlib
import dataclasses
import gc
import io
import math
import numbers
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from typing import Any


_DEFAULTS = {
    "film": "kodak_portra_400",
    "paper": "kodak_portra_endura",
    "exposure_ev": 0.0,
    "print_exposure": 1.0,
    "grain": 1.0,
    "halation": 1.0,
    "dir_couplers": 1.0,
    "auto_exposure": True,
}
_SLUG = re.compile(r"[a-z][a-z0-9_]*\Z")


def _validate_settings(settings: dict) -> dict:
    if not isinstance(settings, dict):
        raise ValueError("spektrafilm settings must be a dictionary")
    unknown = sorted(set(settings) - set(_DEFAULTS), key=str)
    if unknown:
        raise ValueError("Unsupported spektrafilm setting(s): " + ", ".join(map(str, unknown)))
    result = {**_DEFAULTS, **settings}
    for key in ("film", "paper"):
        value = result[key]
        if not isinstance(value, str) or not _SLUG.fullmatch(value):
            raise ValueError(f"{key} must be an exact lowercase stock slug, for example {_DEFAULTS[key]!r}")
    for key in ("exposure_ev", "print_exposure", "grain", "halation", "dir_couplers"):
        value = result[key]
        if isinstance(value, bool) or not isinstance(value, numbers.Real) or not math.isfinite(value):
            raise ValueError(f"{key} must be a finite number; got {value!r}")
        result[key] = float(value)
    for key in ("grain", "halation", "dir_couplers"):
        if not 0.0 <= result[key] <= 1.0:
            raise ValueError(f"{key} must be in [0, 1]; got {result[key]}")
    # Upstream's GUI exposure interval. The outer product may intentionally
    # expose a narrower interval; these are runtime validation bounds.
    if not -100.0 <= result["exposure_ev"] <= 100.0:
        raise ValueError("exposure_ev must be in [-100, 100] EV")
    if not 0.0 <= result["print_exposure"] <= 64.0:
        raise ValueError("print_exposure must be in [0, 64] (linear multiplier)")
    if not isinstance(result["auto_exposure"], bool):
        raise ValueError("auto_exposure must be a JSON boolean, not a string or number")
    return result


def _plain(value: Any) -> Any:
    """Keep metadata JSON-compatible without importing NumPy at module import."""
    if dataclasses.is_dataclass(value):
        return {field.name: _plain(getattr(value, field.name)) for field in dataclasses.fields(value)}
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if hasattr(value, "tolist"):
        return value.tolist()
    if hasattr(value, "item"):
        return value.item()
    return value


def _params_snapshot(params) -> dict:
    result = {}
    for field in dataclasses.fields(params):
        value = getattr(params, field.name)
        if field.name in ("film", "print"):
            result[field.name] = _plain(value.info)
        else:
            result[field.name] = _plain(value)
    return result


def _engine(repo: Path):
    source = repo / "src"
    expected = source / "spektrafilm" / "__init__.py"
    if not expected.is_file():
        raise ValueError(f"runtime.film_repo is not a spektrafilm checkout: expected {expected}")
    cached = sys.modules.get("spektrafilm")
    if cached is not None:
        cached_file = getattr(cached, "__file__", None)
        if cached_file is None or Path(cached_file).resolve() != expected.resolve():
            raise RuntimeError("A different spektrafilm checkout is already imported; render in a fresh Python process")
    # The caller's stdout may be a machine-readable JSON channel. Capture
    # upstream informational prints and surface them in metadata instead.
    messages = io.StringIO()
    original_path = list(sys.path)
    try:
        sys.path.insert(0, str(source))
        with contextlib.redirect_stdout(messages):
            import numpy as np
            import colour
            from PIL import Image, ImageCms, ImageOps
            import spektrafilm
            from spektrafilm import Simulator, digest_params, init_params
            from spektrafilm.profiles.io import list_profiles, load_profile
    except ImportError as exc:
        raise RuntimeError(
            f"spektrafilm dependencies are unavailable in {sys.executable}. "
            "Run the outer CLI with its configured film Python environment; "
            f"import failed: {exc}"
        ) from exc
    finally:
        sys.path[:] = original_path
    return np, colour, Image, ImageCms, ImageOps, spektrafilm, Simulator, digest_params, init_params, list_profiles, load_profile, messages


def _repo_commit(repo: Path) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5, check=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        commit = completed.stdout.strip()
        return commit if re.fullmatch(r"[0-9a-fA-F]{40,64}", commit) else None
    except (OSError, subprocess.SubprocessError):
        return None


def render(input_path: Path, output_path: Path, settings: dict, runtime: dict) -> dict:
    """Render one local proxy, returning JSON-compatible facts and warnings.

    Inputs: opaque 8-bit display RGB JPEG/PNG; unprofiled proxies are assumed
    sRGB, embedded profiles are converted to sRGB. No resize/crop occurs here.
    Outputs: .png = lossless 8-bit sRGB; .jpg/.jpeg = JPEG90, 4:4:4, sRGB ICC.

    ``grain`` is an explicitly documented particle-area approximation of a
    continuous noise amplitude. It is not an upstream ``grain.amount`` field.
    No clipping statistic is used to grade aesthetics or reject a finite image.
    """
    started = time.perf_counter()
    resolved = _validate_settings(settings)
    if not isinstance(runtime, dict) or not runtime.get("film_repo"):
        raise ValueError("runtime.film_repo must name the local spektrafilm checkout")
    input_path = Path(input_path).expanduser().resolve()
    output_path = Path(output_path).expanduser().resolve()
    repo = Path(runtime["film_repo"]).expanduser().resolve()
    if not input_path.is_file():
        raise FileNotFoundError(f"Input proxy does not exist: {input_path}")
    if input_path == output_path:
        raise ValueError("Input and output paths must differ; source proxies are never overwritten")
    extension = output_path.suffix.lower()
    if extension not in (".png", ".jpg", ".jpeg"):
        raise ValueError("spektrafilm output_path must end in .png, .jpg or .jpeg")
    warnings: list[str] = []
    (np, colour, Image, ImageCms, ImageOps, engine_module, Simulator,
     digest_params, init_params, list_profiles, load_profile, messages) = _engine(repo)
    saved_rng = np.random.get_state()
    simulator = None
    linear_input = linear_output = encoded = pixels = None
    temporary = None
    try:
        with contextlib.redirect_stdout(messages):
            available = list_profiles()
            for key in ("film", "paper"):
                if resolved[key] not in available:
                    raise ValueError(f"Unknown {key} profile {resolved[key]!r}; not bundled in {repo}")
            film_profile = load_profile(resolved["film"])
            paper_profile = load_profile(resolved["paper"])
            if not film_profile.is_filming or not film_profile.is_film:
                raise ValueError(f"{resolved['film']!r} is not a filming-stage film profile")
            if not paper_profile.is_printing:
                raise ValueError(f"{resolved['paper']!r} is not a printing-stage profile")
            if film_profile.info.channel_model != paper_profile.info.channel_model:
                raise ValueError("Film and paper must use the same channel model (color or black-and-white)")
            params = digest_params(init_params(film_profile=resolved["film"], print_profile=resolved["paper"]))

        params.camera.exposure_compensation_ev = resolved["exposure_ev"]
        params.camera.auto_exposure = resolved["auto_exposure"]
        params.enlarger.print_exposure = resolved["print_exposure"]
        params.io.input_color_space = "ProPhoto RGB"
        params.io.input_cctf_decoding = False
        params.io.output_color_space = "sRGB"
        params.io.output_cctf_encoding = False
        params.settings.use_enlarger_lut = True
        params.settings.use_scanner_lut = True
        params.settings.lut_resolution = 33
        params.settings.preview_mode = False
        params.debug.lut_mode = False

        grain = params.film_render.grain
        base_area = float(grain.particle_area_um2)
        base_micro = tuple(grain.micro_structure)
        strength = resolved["grain"]
        effective_grain = max(strength, .001) if strength > 0 else 0.0
        grain.active = strength > 0
        if strength > 0:
            grain.particle_area_um2 = base_area * effective_grain ** 2
            grain.micro_structure = (base_micro[0], base_micro[1] * effective_grain)
        if 0 < strength < .001:
            warnings.append("GRAIN_MINIMUM: positive grain below 0.001 uses 0.001 to avoid excessive particle counts")
        halation = params.film_render.halation
        halation.active = resolved["halation"] > 0
        halation.scatter_amount = resolved["halation"]
        halation.halation_amount = resolved["halation"]
        couplers = params.film_render.dir_couplers
        couplers.active = resolved["dir_couplers"] > 0
        couplers.amount = resolved["dir_couplers"]

        input_management = {"unprofiled_assumption": "sRGB", "embedded_icc": False, "icc_converted_to_srgb": False}
        with Image.open(input_path) as opened:
            if opened.format not in ("JPEG", "PNG") or opened.mode not in ("RGB", "L"):
                raise ValueError("Expected an opaque 8-bit RGB/grayscale JPEG or PNG proxy; prepare RAW, linear/16-bit TIFF and alpha inputs in the outer CLI first")
            with ImageOps.exif_transpose(opened) as oriented:
                icc = oriented.info.get("icc_profile")
                if icc:
                    input_management["embedded_icc"] = True
                    try:
                        source_profile = ImageCms.ImageCmsProfile(io.BytesIO(icc))
                        converted = ImageCms.profileToProfile(oriented, source_profile, ImageCms.createProfile("sRGB"), outputMode="RGB")
                    except Exception as exc:
                        raise ValueError(f"Cannot convert the proxy's embedded ICC profile to sRGB: {exc}") from exc
                    input_management["icc_converted_to_srgb"] = True
                else:
                    converted = oriented.convert("RGB")
                with converted:
                    width, height = converted.size
                    if width < 1 or height < 1:
                        raise ValueError("Input proxy has no image pixels")
                    proxy_rgb = np.asarray(converted, dtype=np.float64) / 255.0
        # Explicit transfer-function decoding, then linear-primary conversion.
        linear_srgb = colour.cctf_decoding(proxy_rgb, function="sRGB")
        linear_input = colour.RGB_to_RGB(
            linear_srgb, "sRGB", "ProPhoto RGB",
            apply_cctf_decoding=False, apply_cctf_encoding=False,
        )
        del proxy_rgb, linear_srgb
        np.random.seed(20261102)
        init_started = time.perf_counter()
        with contextlib.redirect_stdout(messages):
            simulator = Simulator(params)
        init_s = time.perf_counter() - init_started
        process_started = time.perf_counter()
        with contextlib.redirect_stdout(messages):
            linear_output = np.asarray(simulator.process(linear_input))
        process_s = time.perf_counter() - process_started
        if linear_output.shape != (height, width, 3):
            raise RuntimeError(f"Unexpected engine output shape {linear_output.shape}; expected {(height, width, 3)}")
        finite = np.isfinite(linear_output)
        nonfinite = int(np.count_nonzero(~finite))
        if nonfinite:
            raise RuntimeError(f"spektrafilm produced {nonfinite} non-finite channel values; no output was saved")
        below = linear_output < 0
        above = linear_output > 1
        clipped = below | above
        metrics = {
            "finite": True, "nonfinite_channels": 0,
            "linear_min": float(linear_output.min()), "linear_max": float(linear_output.max()),
            "linear_channel_means": linear_output.mean(axis=(0, 1)).tolist(),
            "below_zero_channels": int(np.count_nonzero(below)),
            "above_one_channels": int(np.count_nonzero(above)),
            "clipped_channels": int(np.count_nonzero(clipped)),
            "clipped_channels_fraction": float(clipped.mean()),
            "clipped_pixels": int(np.count_nonzero(np.any(clipped, axis=2))),
            "clipped_pixels_fraction": float(np.any(clipped, axis=2).mean()),
        }
        if metrics["clipped_channels"]:
            warnings.append(
                f"LINEAR_RANGE_CLIPPED: {metrics['clipped_channels_fraction']:.6%} of channel values "
                f"were outside [0,1] before sRGB encoding (min={metrics['linear_min']:.6g}, "
                f"max={metrics['linear_max']:.6g}); this is a numeric range report, not an aesthetic failure"
            )
        encoded = colour.cctf_encoding(np.clip(linear_output, 0, 1), function="sRGB")
        pixels = np.rint(np.clip(encoded, 0, 1) * 255).astype(np.uint8)
        metrics["encoded_std"] = float(pixels.std())
        metrics["black_pixels_fraction"] = float(np.mean(np.max(pixels, axis=2) <= 1))
        metrics["white_pixels_fraction"] = float(np.mean(np.min(pixels, axis=2) >= 254))
        output_icc = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(prefix=".film-", suffix=extension, dir=output_path.parent, delete=False) as handle:
            temporary = Path(handle.name)
        with Image.fromarray(pixels) as output_image:
            if extension == ".png":
                output_image.save(temporary, format="PNG", compress_level=6, icc_profile=output_icc)
            else:
                output_image.save(temporary, format="JPEG", quality=90, subsampling=0, optimize=True, icc_profile=output_icc)
        with Image.open(temporary) as verified:
            verified.load()
            if verified.size != (width, height) or verified.mode != "RGB" or not verified.info.get("icc_profile"):
                raise RuntimeError("Saved output failed RGB/size/ICC verification")
        os.replace(temporary, output_path)
        temporary = None
        upstream_messages = [line.strip() for line in messages.getvalue().splitlines() if line.strip()]
        warnings.extend("UPSTREAM: " + line for line in upstream_messages)
        return {
            "engine": "spektrafilm", "status": "ok", "output_path": str(output_path),
            "width": width, "height": height,
            "elapsed_s": time.perf_counter() - started, "process_s": process_s, "initialization_s": init_s,
            "output_bytes": output_path.stat().st_size,
            "output_format": "PNG" if extension == ".png" else "JPEG",
            "output_color_space": "sRGB", "output_bit_depth": 8,
            "resolved_settings": resolved, "resolved_params": _params_snapshot(params),
            "control_mapping": {
                "grain": {"method": "particle_area_scaled_by_strength_squared", "requested_strength": strength,
                          "effective_strength": effective_grain, "upstream_base_particle_area_um2": base_area,
                          "particle_area_um2": float(grain.particle_area_um2), "micro_structure": list(grain.micro_structure),
                          "meaning": "0 bypasses grain; 1 is upstream baseline; intermediate values approximate RMS amplitude by changing particle count/area, not a native amount control; exact RMS and cloud size are not guaranteed linear"},
                "halation": "active=(h>0); scatter_amount=h and halation_amount=h; stock strengths and physical scales unchanged",
                "dir_couplers": "active=(d>0); native dir_couplers.amount=d; stock inhibition matrix unchanged",
                "exposure_ev": "native camera.exposure_compensation_ev; upstream print exposure compensation remains enabled",
                "profile_modifications": False,
            },
            "input_color_management": input_management,
            "runtime": {"film_repo": str(repo), "source_commit": _repo_commit(repo), "python": sys.executable,
                        "engine_module": str(engine_module.__file__), "lut_resolution": 33,
                        "seed": 20261102, "seed_scope": "numpy/scipy plus upstream fixed grain layer seeds; numba glare is not fully deterministic"},
            "metrics": metrics, "warnings": warnings,
        }
    finally:
        np.random.set_state(saved_rng)
        if temporary is not None:
            with contextlib.suppress(OSError):
                temporary.unlink()
        # No persistent simulator/cache lives in this adapter. The process
        # owner decides when to exit; Python releases the render references.
        simulator = linear_input = linear_output = encoded = pixels = None
        # The upstream stage/service graph contains bound-method cycles.
        # Collect them between renders in a long-lived outer batch process.
        gc.collect()
