"""High-precision, deterministic spektrafilm passes for a conditional-layer PSD.

The input is linear-light SDR sRGB.  Every pass is rendered by the actual film,
paper and scanner pipeline; returned arrays are floating-point encoded sRGB,
never an 8-bit intermediary.  This controlled PSD mode disables unexposed
glare, scan sharpening and other lens effects, and records that policy.
"""
from __future__ import annotations

import contextlib
import copy
import gc
import inspect
import math
import os
from pathlib import Path
import tempfile
import threading
import time
import warnings as warning_module

from film_backend import _engine, _params_snapshot, _repo_commit, _validate_settings


_LOCK = threading.RLock()
_DEFAULT_SEED = 20261112
_STRIP_SAMPLES = 524_288
_STAGES = (
    ("base", "01 胶片与相纸基础"),
    ("exposure", "02 自动与手动曝光"),
    ("halation", "03 乳剂散射与光晕"),
    ("dir", "04 DIR 显影抑制与扩散"),
    ("grain", "05 胶片颗粒"),
)


class ResourceLimitError(RuntimeError):
    """An actual resource budget prevents a whole-frame reference render."""
    code = "RESOURCE_LIMIT"


def _rows(shape):
    height, width = shape[:2]
    size = max(1, _STRIP_SAMPLES // (width * 3))
    for y in range(0, height, size):
        yield slice(y, min(height, y + size))


def _frame(np, shape, runtime, role, dtype="float32"):
    """Create a registered disk-backed frame when the caller owns a work dir."""
    directory = runtime.get("work_dir") or runtime.get("stage_dir")
    if not directory:
        return np.empty(shape, dtype=dtype)
    directory = Path(directory).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix=f"film-{role}-", suffix=".npy", dir=directory, delete=False) as handle:
        path = Path(handle.name)
    array = np.lib.format.open_memmap(path, mode="w+", dtype=dtype, shape=shape)
    registry = runtime.get("_memmaps")
    if isinstance(registry, list):
        registry.append(array)
    return array


def _available_memory():
    if os.name == "nt":
        import ctypes
        class Status(ctypes.Structure):
            _fields_ = [("length", ctypes.c_uint32), ("load", ctypes.c_uint32),
                        ("total_physical", ctypes.c_uint64), ("available_physical", ctypes.c_uint64),
                        ("total_commit", ctypes.c_uint64), ("available_commit", ctypes.c_uint64),
                        ("total_virtual", ctypes.c_uint64), ("available_virtual", ctypes.c_uint64),
                        ("available_extended_virtual", ctypes.c_uint64)]
        status = Status(); status.length = ctypes.sizeof(status)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return {"physical_available_bytes": int(status.available_physical),
                    "commit_available_bytes": int(status.available_commit)}
    try:
        return {"physical_available_bytes": int(os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE"))}
    except (AttributeError, OSError, ValueError):
        return {}


def _resource_policy(shape, runtime):
    """A dynamic whole-frame working-set estimate, never a fixed pixel cap.

    The upstream reference process allocates several float64 RGB, density and
    grain arrays.  Disk-backed stage retention does not make that process
    tiled.  The estimate is deliberately conservative and disclosed rather
    than risking process/system OOM or silently changing the model.
    """
    pixels = int(shape[0]) * int(shape[1])
    estimated = pixels * 768 + 256 * 1024**2
    available = _available_memory()
    explicit = runtime.get("python_memory_budget_bytes")
    if explicit is not None:
        if isinstance(explicit, bool) or not isinstance(explicit, int) or explicit <= 0:
            raise ValueError("python_memory_budget_bytes must be a positive integer")
        budget = explicit
        origin = "explicit runtime.python_memory_budget_bytes"
    else:
        candidates = [available.get("physical_available_bytes"), available.get("commit_available_bytes")]
        candidates = [value for value in candidates if value is not None]
        budget = int(min(candidates) * .80) if candidates else None
        origin = "80% of current available physical RAM and commit budget"
    result = {"estimated_peak_working_bytes": estimated, "budget_bytes": budget,
              "estimate_bytes_per_pixel": 768, "policy": origin, **available,
              "algorithm": "whole-frame original reference; not naive independent tiles"}
    if budget is not None and estimated > budget:
        raise ResourceLimitError(
            f"Whole-frame spektrafilm resource estimate {estimated / 1024**3:.2f} GiB exceeds "
            f"the current {budget / 1024**3:.2f} GiB budget. No fixed resolution cap is applied; "
            "use the native SpektraLab backend, free resources, or explicitly choose a smaller output. "
            "Spatial effects were not silently tiled or disabled.")
    return result


@contextlib.contextmanager
def _deterministic_grain(np, grain_module, seed):
    """Retain the upstream stochastic model with controlled local sampling.

    Upstream hard-codes layer seeds and uses a separately seeded Numba RNG for
    microstructure.  Temporary in-memory wrappers offset layer seeds and use a
    local Generator for the same mean/std lognormal distribution.  Source and
    profile files are never edited; the lock covers all wrapper lifetimes.
    """
    original_particle = grain_module.layer_particle_model
    original_lognormal = grain_module.fast_lognormal_from_mean_std
    signature = inspect.signature(original_particle)
    state = np.random.get_state()
    micro_rng = np.random.default_rng(seed ^ 0x6A09E667)

    def particle(*args, **kwargs):
        bound = signature.bind_partial(*args, **kwargs)
        original_seed = bound.arguments.get("seed")
        bound.arguments["seed"] = (seed + int(original_seed or 0)) % (2**32)
        return original_particle(*bound.args, **bound.kwargs)

    def lognormal_from_mean_std(mean_arr, std_arr):
        mean = np.asarray(mean_arr, dtype=np.float64)
        std = np.asarray(std_arr, dtype=np.float64)
        if np.any(mean < 0) or np.any(std < 0):
            raise ValueError("Lognormal mean/std must be nonnegative")
        ratio = np.divide(std, mean, out=np.zeros_like(std), where=mean > 0)
        variance_log = np.log1p(ratio * ratio)
        mu = np.log(np.maximum(mean, np.finfo(np.float64).tiny)) - variance_log / 2
        sample = micro_rng.lognormal(mu, np.sqrt(variance_log))
        return np.where(mean > 0, sample, 0.0)

    try:
        np.random.seed(seed)
        grain_module.layer_particle_model = particle
        grain_module.fast_lognormal_from_mean_std = lognormal_from_mean_std
        yield
    finally:
        grain_module.layer_particle_model = original_particle
        grain_module.fast_lognormal_from_mean_std = original_lognormal
        np.random.set_state(state)


def _configure_common(params):
    params.io.input_color_space = "ProPhoto RGB"
    params.io.input_cctf_decoding = False
    params.io.output_color_space = "sRGB"
    params.io.output_cctf_encoding = False
    params.io.crop = False
    params.io.upscale_factor = 1.0
    params.camera.auto_exposure = False  # Measured once outside the passes.
    params.camera.lens_blur_um = 0.0
    params.camera.diffusion_filter.active = False
    params.enlarger.lens_blur = 0.0
    params.enlarger.diffusion_filter.active = False
    params.scanner.lens_blur = 0.0
    params.scanner.unsharp_mask = (0.0, 0.0)
    params.film_render.glare.active = False
    params.print_render.glare.active = False
    params.film_render.halation.boost_ev = 0.0
    params.settings.use_enlarger_lut = True
    params.settings.use_scanner_lut = True
    params.settings.lut_resolution = 33
    params.settings.use_fast_stats = False
    params.settings.preview_mode = False
    params.debug.lut_mode = False
    params.debug.deactivate_spatial_effects = False
    params.debug.deactivate_stochastic_effects = False
    params.debug.print_timings = False


def _configure_stage(params, index, resolved):
    has_exposure, has_halation, has_dir, has_grain = (
        index >= 1, index >= 2, index >= 3, index >= 4
    )
    params.camera.exposure_compensation_ev = resolved["exposure_ev"] if has_exposure else 0.0
    params.enlarger.print_exposure = resolved["print_exposure"] if has_exposure else 1.0
    h = resolved["halation"] if has_halation else 0.0
    params.film_render.halation.active = h > 0
    params.film_render.halation.scatter_amount = h
    params.film_render.halation.halation_amount = h
    d = resolved["dir_couplers"] if has_dir else 0.0
    params.film_render.dir_couplers.active = d > 0
    params.film_render.dir_couplers.amount = d
    grain = params.film_render.grain
    strength = resolved["grain"] if has_grain else 0.0
    effective = max(strength, 0.001) if strength > 0 else 0.0
    grain.active = strength > 0
    base_area = float(grain.particle_area_um2)
    base_micro = tuple(grain.micro_structure)
    if effective:
        grain.particle_area_um2 = base_area * effective**2
        grain.micro_structure = (base_micro[0], base_micro[1] * effective)
    return {"grain_strength": strength, "effective_grain_strength": effective,
            "base_particle_area_um2": base_area,
            "particle_area_um2": float(grain.particle_area_um2),
            "micro_structure": list(grain.micro_structure),
            "halation_strength": h, "dir_strength": d,
            "manual_exposure_ev": float(params.camera.exposure_compensation_ev),
            "print_exposure": float(params.enlarger.print_exposure)}


def render_passes(linear_srgb, settings: dict, runtime: dict) -> dict:
    """Return base, exposure, halation, DIR and grain final-RGB renderings.

    Stage differences are conditional on this order and the input; they are not
    independent physical operators.  The caller controls postprocessing and
    PSD quantization.  A failure raises and never returns partial success.
    """
    if not isinstance(settings, dict):
        raise ValueError("settings must be an object")
    supplied = dict(settings)
    seed = supplied.pop("seed", _DEFAULT_SEED)
    if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed < 2**32:
        raise ValueError("seed must be an integer in [0, 4294967295]")
    resolved = _validate_settings(supplied)
    if not isinstance(runtime, dict) or not runtime.get("film_repo"):
        raise ValueError("runtime.film_repo must name the spektrafilm checkout")
    repo = Path(runtime["film_repo"]).expanduser().resolve()
    with _LOCK:
        return _render_locked(linear_srgb, resolved, seed, runtime, repo)


def _render_locked(linear_srgb, resolved, seed, runtime, repo):
    started = time.perf_counter()
    (np, colour, _Image, _ImageCms, _ImageOps, engine_module, Simulator,
     digest_params, init_params, list_profiles, load_profile, messages) = _engine(repo)
    import spektrafilm.model.grain as grain_module
    from spektrafilm.runtime.services.resize import ResizingService
    from spektrafilm.utils.autoexposure import measure_autoexposure_ev

    if not isinstance(linear_srgb, np.ndarray) or linear_srgb.ndim != 3 or linear_srgb.shape[2] != 3:
        raise ValueError("linear_srgb must be an HWC NumPy array with three channels")
    if not np.issubdtype(linear_srgb.dtype, np.floating):
        raise TypeError("linear_srgb must be floating point")
    for rows in _rows(linear_srgb.shape):
        part = linear_srgb[rows]
        if not np.isfinite(part).all() or np.any(part < 0) or np.any(part > 1):
            raise ValueError("linear_srgb must be finite SDR data in [0,1]")
    height, width = linear_srgb.shape[:2]
    if height < 1 or width < 1:
        raise ValueError("spektrafilm input must contain at least one pixel")
    resource_policy = _resource_policy(linear_srgb.shape, runtime)

    warnings = [
        "PSD_CONTROLLED_MODE: glare, scanner sharpening and unexposed lens/diffusion effects are disabled; this is not a pixel-equivalent clone of the legacy flat grade mode.",
        "CONDITIONAL_LAYERS: stage RGBs follow base -> exposure -> halation -> DIR -> grain; changing cached layers does not rerun the physical model.",
    ]
    if 0 < resolved["grain"] < 0.001:
        warnings.append("GRAIN_MINIMUM: positive grain below 0.001 uses 0.001")
    saved_rng = np.random.get_state()
    stages = []
    simulator = None
    with warning_module.catch_warnings(record=True) as captured:
        warning_module.simplefilter("always")
        try:
            with contextlib.redirect_stdout(messages):
                available = list_profiles()
                for key in ("film", "paper"):
                    if resolved[key] not in available:
                        raise ValueError(f"Unknown {key} profile: {resolved[key]}")
                film = load_profile(resolved["film"])
                paper = load_profile(resolved["paper"])
                if not film.is_filming or not film.is_film or not paper.is_printing:
                    raise ValueError("Expected a filming-stage film and printing-stage paper")
                if film.info.channel_model != paper.info.channel_model:
                    raise ValueError("Film and paper channel models differ")
                common = digest_params(init_params(film_profile=resolved["film"], print_profile=resolved["paper"]))
            _configure_common(common)

            # Only the true reference process remains whole-frame.  Conversion,
            # retained stage RGBs and their transport can be safely striped,
            # because these are pointwise operations.
            linear_input = _frame(np, linear_srgb.shape, runtime, "linear-prophoto", "float64")
            for rows in _rows(linear_srgb.shape):
                converted = colour.RGB_to_RGB(
                    np.asarray(linear_srgb[rows], dtype=np.float64), "sRGB", "ProPhoto RGB",
                    apply_cctf_decoding=False, apply_cctf_encoding=False)
                if not np.isfinite(converted).all():
                    raise RuntimeError("Input primary conversion produced nonfinite values")
                linear_input[rows] = converted
            if isinstance(linear_input, np.memmap):
                linear_input.flush()
            ae_ev = 0.0
            if resolved["auto_exposure"]:
                service = ResizingService(common.io, common.camera.film_format_mm)
                with contextlib.redirect_stdout(messages):
                    ae_ev = float(measure_autoexposure_ev(
                        service.small_preview(linear_input), "ProPhoto RGB", False,
                        method=common.camera.auto_exposure_method,
                    ))
                if not math.isfinite(ae_ev):
                    raise RuntimeError("Automatic exposure returned a nonfinite EV")
            gain = 2.0**ae_ev
            if not math.isfinite(gain):
                raise RuntimeError("Automatic exposure gain is not finite")
            exposed_input = linear_input
            if gain != 1.0:
                exposed_input = _frame(np, linear_srgb.shape, runtime, "exposed-prophoto", "float64")
                for rows in _rows(linear_srgb.shape):
                    exposed_input[rows] = linear_input[rows] * gain
                if isinstance(exposed_input, np.memmap):
                    exposed_input.flush()

            for index, (key, name) in enumerate(_STAGES):
                stage_start = time.perf_counter()
                params = copy.deepcopy(common)
                controls = _configure_stage(params, index, resolved)
                # Use a fresh controlled RNG context for every stage so hidden
                # sampling cannot advance a later stage's random realization.
                with _deterministic_grain(np, grain_module, seed):
                    with contextlib.redirect_stdout(messages):
                        simulator = Simulator(params)
                        result = np.asarray(simulator.process(
                            (exposed_input if index else linear_input).copy()
                        ))
                if result.shape != (height, width, 3):
                    raise RuntimeError(f"{key} render has invalid dimensions")
                encoded = _frame(np, result.shape, runtime, key)
                clipped_count = 0
                minimum, maximum = math.inf, -math.inf
                for rows in _rows(result.shape):
                    part = np.asarray(result[rows], dtype=np.float64)
                    if not np.isfinite(part).all():
                        raise RuntimeError(f"{key} render has nonfinite pixels")
                    clipped_count += int(np.count_nonzero((part < 0) | (part > 1)))
                    minimum, maximum = min(minimum, float(part.min())), max(maximum, float(part.max()))
                    encoded[rows] = np.clip(colour.cctf_encoding(np.clip(part, 0, 1), function="sRGB"), 0, 1)
                if isinstance(encoded, np.memmap):
                    encoded.flush()
                clip_fraction = clipped_count / result.size
                snapshot = _params_snapshot(params)
                stages.append({"key": key, "name": name, "rgb": encoded,
                    "metadata": {"elapsed_s": time.perf_counter()-stage_start,
                        "input_auto_exposure_ev": ae_ev if index else 0.0,
                        "input_auto_exposure_gain": gain if index else 1.0,
                        "controls": controls, "resolved_params": snapshot,
                        "linear_min": minimum, "linear_max": maximum,
                        "linear_clipped_channel_fraction": clip_fraction,
                        "output_precision": "float32 encoded sRGB; no 8-bit intermediate",
                        "file_backed": isinstance(encoded, np.memmap)}})
                if clip_fraction:
                    warnings.append(f"{key}: {clip_fraction:.6%} linear channel values clipped to SDR range; numeric report only")
                simulator = result = None
                gc.collect()
            warnings.extend("UPSTREAM: " + str(w.message) for w in captured)
            warnings.extend("UPSTREAM: " + line.strip() for line in messages.getvalue().splitlines() if line.strip())
            # Repeated colour-science warnings do not carry more information.
            warnings = list(dict.fromkeys(warnings))
            return {"stages": stages, "warnings": warnings,
                "metadata": {"engine": "spektrafilm", "engine_version": getattr(engine_module, "__version__", None),
                    "source_commit": _repo_commit(repo), "film_repo": str(repo),
                    "elapsed_s": time.perf_counter()-started, "size": [width, height],
                    "resolved_settings": {**resolved, "seed": seed},
                    "output_color_space": "encoded sRGB", "output_precision": "float32",
                    "retained_stage_storage": "disk .npy memmaps" if runtime.get("work_dir") or runtime.get("stage_dir") else "RAM arrays (caller provided no work_dir)",
                    "resource_policy": resource_policy,
                    "internal_lut_resolution": 33,
                    "auto_exposure": {"requested": resolved["auto_exposure"],
                        "measurement_space": "linear ProPhoto RGB",
                        "method": common.camera.auto_exposure_method,
                        "measured_once": True, "ev": ae_ev, "gain": gain,
                        "manual_ev_is_separate": True},
                    "controlled_mode_disabled": ["film_glare", "print_glare", "scanner_unsharp_mask",
                        "camera_lens_blur", "enlarger_lens_blur", "scanner_lens_blur",
                        "camera_diffusion_filter", "enlarger_diffusion_filter", "highlight_boost"],
                    "rng": {"seed": seed, "particle_method": "upstream Poisson-Binomial with layer seed offset",
                        "microstructure_method": "same mean/std lognormal formula, local NumPy Generator instead of Numba RNG",
                        "source_files_modified": False, "global_numpy_state_restored": True},
                    "profile_modifications": False,
                    "grain_mapping": "particle_area = stock_area * max(strength,.001)^2; microstructure amplitude *= strength"}}
        except MemoryError as exc:
            raise ResourceLimitError("The original whole-frame spektrafilm computation exhausted available memory. No downscale, effect removal or naive spatial tiling was substituted.") from exc
        finally:
            np.random.set_state(saved_rng)
            simulator = None
            gc.collect()
