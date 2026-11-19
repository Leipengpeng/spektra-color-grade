"""Local film grading entry point. Stdout contains exactly one JSON result."""
from __future__ import annotations
import argparse
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from preferences import PRESETS, RANGES, compile_preferences, detect_style
from catalogue import menu, effect_preset, describe_selection

SKILL = Path(__file__).resolve().parents[1]
SUPPORTED = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".bmp"}
REQUEST_KEYS = {"input", "output", "intent", "effect", "preset", "engine", "max_edge", "format", "adjustments", "recipe"}
ENGINE_KEYS = {"film", "paper", "exposure_ev", "print_exposure", "grain", "halation", "dir_couplers", "auto_exposure"}

class GradeError(Exception):
    def __init__(self, code, message, remedy=""):
        super().__init__(message)
        self.code, self.remedy = code, remedy

def runtime_config(root=None):
    config = SKILL / "runtime.local.json"
    stored = json.loads(config.read_text(encoding="utf-8-sig")) if config.exists() else {}
    base = Path(root or os.environ.get("SPEKTRA_RUNTIME_ROOT") or stored.get("root") or Path.home()/"Desktop"/"SpektraTest").expanduser().resolve()
    py = base/".venv-film"/("Scripts/python.exe" if os.name == "nt" else "bin/python")
    values = {"root": str(base), "python": str(py), "film_repo": str(base/"repos"/"spektrafilm"),
              "lab_host": str(base/"apps"/"SpektraLab"/"spektralab-host.exe"),
              "lab_resources": str(base/"apps"/"SpektraLab"/"host"/"engine")}
    for key in values:
        if key != "root" and key in stored and not root and not os.environ.get("SPEKTRA_RUNTIME_ROOT"):
            values[key] = stored[key]
    return values

def backend_available(name, runtime):
    if name == "spektralab":
        return os.name == "nt" and Path(runtime["lab_host"]).is_file() and Path(runtime["lab_resources"]).is_dir()
    return (Path(runtime["film_repo"])/"src"/"spektrafilm").is_dir()

def emit(value):
    print(json.dumps(value, ensure_ascii=False, allow_nan=False))

def interpreter(runtime):
    configured = Path(runtime["python"])
    return str(configured) if configured.is_file() else sys.executable

def doctor(runtime):
    # Probe in the configured interpreter, including a real float ICC transform:
    # an import-only check cannot establish that the shared CMM library loads.
    probe_code = f"""
import json, importlib.util, sys
sys.path.insert(0, {str(SKILL / 'scripts')!r})
result = {{'modules': {{n: bool(importlib.util.find_spec(n)) for n in ['PIL','numpy','colour','scipy','numba','tifffile','OpenImageIO','psd_tools']}}}}
try:
    import numpy as np
    from PIL import ImageCms
    from icc_float import FloatICCTransform
    profile = ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes()
    with FloatICCTransform(profile) as transform:
        converted = transform.apply(np.array([[[0.123,0.456,0.789]]], dtype=np.float32))
        if converted.dtype != np.float32 or not np.isfinite(converted).all():
            raise RuntimeError('Float ICC smoke conversion failed')
        result['float_icc'] = {{'ready': True, 'metadata': transform.metadata}}
except Exception as exc:
    result['float_icc'] = {{'ready': False, 'code': getattr(exc, 'code', 'ICC_DEPENDENCY_PROBE_FAILED'), 'error': str(exc),
        'remedy': 'Install LittleCMS 2 (or an existing GIMP/Krita runtime), or set SPEKTRA_LCMS2 to its shared-library path. RGB ICC inputs require this float conversion; no 8-bit fallback is used.'}}
print(json.dumps(result))
"""
    probe_env = os.environ.copy()
    probe_env["SPEKTRA_RUNTIME_ROOT"] = str(runtime["root"])
    try:
        probe = subprocess.run([interpreter(runtime), "-c", probe_code], env=probe_env,
                               capture_output=True, text=True, timeout=60)
        result = json.loads(probe.stdout) if probe.returncode == 0 else {}
        probe_error = probe.stderr.strip() if probe.returncode else ""
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        result, probe_error = {}, str(exc)
    modules = result.get("modules", {})
    float_icc = result.get("float_icc", {"ready": False, "code": "ICC_DEPENDENCY_PROBE_FAILED",
        "error": probe_error or "Configured Python dependency probe did not return a result",
        "remedy": "Check the configured Python environment and install LittleCMS 2; set SPEKTRA_LCMS2 to its shared-library path if needed."})
    images_ready = all(modules.get(n) for n in ["PIL", "numpy"])
    lab = images_ready and backend_available("spektralab", runtime)
    film = images_ready and backend_available("spektrafilm", runtime) and all(modules.get(n) for n in ["colour", "scipy", "numba"])
    psd_ready = (lab or film) and all(modules.get(n) for n in ['tifffile','OpenImageIO','psd_tools'])
    return {"status": "ready" if lab or film else "blocked", "runtime": runtime,
            "python": interpreter(runtime), "modules": modules,
            "backends": {"spektralab": lab, "spektrafilm": film},
            "legacy_8bit_ready": lab or film,
            "psd_ready": psd_ready,
            "float_icc_ready": bool(float_icc.get("ready")), "float_icc": float_icc,
            "psd_icc_ready": psd_ready and bool(float_icc.get("ready")),
            "psd_input_readiness": {"untagged_srgb": psd_ready, "embedded_rgb_icc": psd_ready and bool(float_icc.get("ready"))},
            "check_scope": "files, Python imports, and a real LittleCMS float ICC conversion; GPU execution is checked during grading",
            "remedy": "Use configure --root PATH for an existing SpektraTest runtime. See references/integration.md for a new machine."}

def validate_request(request):
    if not isinstance(request, dict):
        raise GradeError("INVALID_REQUEST", "Request must be a JSON object")
    unknown = set(request) - REQUEST_KEYS
    if unknown:
        raise GradeError("INVALID_REQUEST", "Unknown request keys: " + ", ".join(sorted(unknown)))
    for name in ["input", "output"]:
        if not isinstance(request.get(name), str) or not request[name].strip():
            raise GradeError("INVALID_REQUEST", f"{name} must be a nonempty local path")
    if not isinstance(request.get("intent", ""), str):
        raise GradeError("INVALID_REQUEST", "intent must be a string")
    if request.get("engine", "auto") not in ["auto", "spektralab", "spektrafilm"]:
        raise GradeError("INVALID_REQUEST", "engine must be auto, spektralab or spektrafilm")
    edge = request.get("max_edge", 1600)
    if isinstance(edge, bool) or not isinstance(edge, int) or edge != 0 and not 256 <= edge <= 8192:
        raise GradeError("INVALID_REQUEST", "max_edge must be 0 (original size) or an integer from 256 to 8192")
    if request.get("format", "jpg") not in ["jpg", "png"]:
        raise GradeError("INVALID_REQUEST", "format must be jpg or png")
    if not isinstance(request.get("adjustments", {}), dict):
        raise GradeError("INVALID_REQUEST", "adjustments must be an object")
    source = Path(request["input"]).expanduser().resolve()
    output = Path(request["output"]).expanduser().resolve()
    if not source.is_file():
        raise GradeError("INPUT_MISSING", f"Input file does not exist: {source}")
    if source.suffix.lower() not in SUPPORTED:
        raise GradeError("UNSUPPORTED_INPUT", "Use an SDR JPEG, PNG, WebP, TIFF or BMP", "Develop RAW/HEIC/HDR into an SDR sRGB image before grading.")
    if output.exists():
        raise GradeError("OUTPUT_EXISTS", f"Output directory already exists: {output}", "Choose a new output directory. Previous results remain available.")
    if output == source.parent or source in output.parents:
        raise GradeError("INVALID_OUTPUT", "Choose a new directory for the graded files")
    return source, output

def prepare_image(source, destination, max_edge):
    from PIL import Image, ImageCms, ImageOps
    warnings = []
    profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB"))
    with Image.open(source) as opened:
        if opened.mode in ["I", "F", "I;16", "I;16B", "I;16L"]:
            raise GradeError("UNSUPPORTED_PRECISION", "This entry point exports 8-bit SDR colour. Convert this high-bit-depth input explicitly first.")
        if opened.format == "TIFF":
            bits = getattr(opened, "tag_v2", {}).get(258, (8,))
            bits = (bits,) if isinstance(bits, int) else bits
            if any(value > 8 for value in bits):
                raise GradeError("UNSUPPORTED_PRECISION", "16-bit TIFF requires an explicit SDR 8-bit conversion for this Skill")
        # PNG RGB16 is exposed as RGB by Pillow. Reject it explicitly instead
        # of silently describing its 8-bit decode as preserved precision.
        if opened.format == "PNG":
            with source.open("rb") as stream:
                header = stream.read(26)
            if len(header) >= 25 and header[24] == 16:
                raise GradeError("UNSUPPORTED_PRECISION", "16-bit PNG requires an explicit SDR 8-bit conversion for this Skill")
        color_profile = opened.info.get("icc_profile")
        image = ImageOps.exif_transpose(opened)
        original_size = list(image.size)
        if image.mode in ["RGBA", "LA"] or "transparency" in image.info:
            rgba = image.convert("RGBA")
            background = Image.new("RGBA", rgba.size, "white")
            background.alpha_composite(rgba)
            image = background.convert("RGB")
            warnings.append("Transparent pixels composited over white for photographic export.")
        if color_profile:
            try:
                image = ImageCms.profileToProfile(image, ImageCms.ImageCmsProfile(io.BytesIO(color_profile)), profile, outputMode="RGB")
            except Exception as exc:
                raise GradeError("ICC_CONVERSION_FAILED", str(exc), "Export an sRGB image from the source application.") from exc
        else:
            image = image.convert("RGB")
            warnings.append("Untagged input interpreted as sRGB.")
        if max_edge:
            image.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
        image.save(destination, icc_profile=profile.tobytes())
        return image.copy(), {"original_oriented_size": original_size, "processing_size": list(image.size),
                              "resized": list(image.size) != original_size, "color_space": "sRGB",
                              "precision": "8-bit SDR", "warnings": warnings}

def finish_image(base, rendered, settings):
    import numpy as np
    from PIL import Image
    pixels = np.asarray(rendered.convert("RGB"), dtype=np.float32)/255.0
    # White balance uses linear-light channel gains. This global treatment is
    # intentionally exposed separately from the material-model parameters.
    linear = np.where(pixels <= 0.04045, pixels/12.92, ((pixels+0.055)/1.055)**2.4)
    warmth, tint = settings["warmth"], settings["tint"]
    gains = np.exp2(np.array([0.30*warmth+0.10*tint, -0.10*tint, -0.30*warmth+0.10*tint]))
    linear = linear*gains*(2**settings["brightness_ev"])
    linear_clipped = float(((linear < 0) | (linear > 1)).mean())
    linear = np.clip(linear, 0.0, 1.0)
    pixels = np.where(linear <= 0.0031308, linear*12.92, 1.055*linear**(1/2.4)-0.055)
    luminance = pixels @ np.array([0.2126, 0.7152, 0.0722])
    pixels = luminance[..., None]+settings["saturation"]*(pixels-luminance[..., None])
    pixels = (pixels-0.5)*settings["contrast"]+0.5
    pixels = pixels*(1-settings["fade"])+settings["fade"]
    original = np.asarray(base, dtype=np.float32)/255.0
    pixels = original*(1-settings["strength"])+pixels*settings["strength"]
    clipped = float(((pixels < 0) | (pixels > 1)).mean())
    result = Image.fromarray(np.round(np.clip(pixels, 0, 1)*255).astype("uint8"))
    return result, {"post_clip_channel_fraction": clipped, "linear_gain_clip_fraction": linear_clipped,
                    "strength_space": "encoded sRGB final blend",
                    "global_adjustments": {key: settings[key] for key in ["brightness_ev", "contrast", "saturation", "warmth", "tint", "fade", "strength"]}}

def compare_sheet(base, result, path, profile):
    from PIL import Image, ImageDraw
    left, right = base.copy(), result.copy()
    left.thumbnail((640, 640)); right.thumbnail((640, 640))
    sheet = Image.new("RGB", (left.width+right.width, max(left.height, right.height)+30), "#171a20")
    sheet.paste(left, (0, 30)); sheet.paste(right, (left.width, 30))
    draw = ImageDraw.Draw(sheet)
    draw.text((10, 8), "INPUT", fill="white"); draw.text((left.width+10, 8), "GRADED", fill="white")
    sheet.save(path, quality=80, optimize=True, icc_profile=profile)

def execute_request(request, runtime=None):
    runtime = runtime or runtime_config()
    previous = None
    if isinstance(request, dict) and request.get("recipe"):
        if not isinstance(request["recipe"], str):
            raise GradeError("INVALID_REQUEST", "recipe must be a local recipe.json path")
        previous = json.loads(Path(request["recipe"]).expanduser().read_text(encoding="utf-8-sig"))
        if previous.get("schema_version") != 1 or not isinstance(previous.get("plan", {}).get("settings"), dict):
            raise GradeError("INVALID_RECIPE", "Use a recipe.json produced by this Skill")
        request = dict(request)
        request.setdefault("input", previous["input"])
        request.setdefault("engine", previous["engine"])
        request.setdefault("max_edge", previous["request"].get("max_edge", 1600))
        request.setdefault("format", previous["request"].get("format", "jpg"))
    source, output = validate_request(request)
    try:
        requested_preset = effect_preset(request.get("effect"), request.get("preset"))
        detected = detect_style(request.get("intent", ""))
        new_preset = requested_preset or (detected if previous and detected != previous["plan"]["preset"] else None)
        inherited = previous["plan"]["settings"] if previous and not new_preset else None
        preset = new_preset or (previous["plan"]["preset"] if inherited else None)
        plan = compile_preferences(request.get("intent", ""), preset, request.get("adjustments"), inherited)
        if request.get("effect") is not None and plan["settings"]["film"] != PRESETS[requested_preset]["film"]:
            raise ValueError("Selected effect and adjustments.film disagree; keep the selected effect's film material")
        plan["selection"] = describe_selection(plan)
    except (ValueError, TypeError) as exc:
        raise GradeError("INVALID_PREFERENCES", str(exc)) from exc
    engine = request.get("engine", "auto")
    candidates = ["spektralab", "spektrafilm"] if engine == "auto" else [engine]
    candidates = [name for name in candidates if backend_available(name, runtime)]
    if not candidates:
        raise GradeError("RUNTIME_MISSING", "No configured render engine is available", "Run doctor and configure --root PATH.")
    output.parent.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    attempts = []
    with tempfile.TemporaryDirectory(prefix=".spektra-", dir=output.parent) as temporary:
        work = Path(temporary)
        bundle = work/"delivery"; bundle.mkdir()
        base, prepared = prepare_image(source, work/"input.png", request.get("max_edge", 1600))
        rendered_path = work/"engine.png"
        for selected in candidates:
            try:
                if selected == "spektrafilm" and base.width * base.height > 4_000_000:
                    raise RuntimeError("spektrafilm is limited to 4 million pixels in this Skill because its full-frame arrays use substantial RAM. Lower max_edge (for example 1600), or explicitly use SpektraLab for a larger export.")
                with contextlib.redirect_stdout(io.StringIO()):
                    if selected == "spektralab":
                        from lab_backend import render
                    else:
                        from film_backend import render
                    engine_settings = {key: plan["settings"][key] for key in ENGINE_KEYS}
                    backend = render(work/"input.png", rendered_path, engine_settings, runtime)
                if backend.get("ok", True) is False:
                    raise RuntimeError(json.dumps(backend.get("error"), ensure_ascii=False))
                if not rendered_path.is_file():
                    raise RuntimeError("Engine did not create its reported output")
                break
            except Exception as exc:
                attempts.append({"engine": selected, "error": str(exc)})
                rendered_path.unlink(missing_ok=True)
        else:
            raise GradeError("RENDER_FAILED", json.dumps(attempts, ensure_ascii=False), "See doctor output and backend availability. Explicit engines do not switch to a different engine.")
        from PIL import Image, ImageCms
        import numpy as np
        with Image.open(rendered_path) as opened:
            opened.load()
            if opened.size != base.size:
                raise GradeError("SIZE_MISMATCH", "Engine changed image dimensions")
            result, post = finish_image(base, opened, plan["settings"])
        profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
        suffix = request.get("format", "jpg")
        result_name = "graded."+suffix
        save_options = {"icc_profile": profile}
        if suffix == "jpg":
            save_options.update(quality=92, subsampling=0, optimize=True)
        result.save(bundle/result_name, **save_options)
        preview = result.copy(); preview.thumbnail((960, 960))
        preview.save(bundle/"preview.jpg", quality=82, optimize=True, icc_profile=profile)
        compare_sheet(base, result, bundle/"comparison.jpg", profile)
        pixels = np.asarray(result)
        constant = bool(np.all(pixels == pixels[0, 0]))
        warnings = plan["warnings"]+prepared["warnings"]+backend.get("warnings", [])
        if constant:
            warnings.append("Constant output image. Inspect the source and result before accepting this grade.")
        final_paths = {"image": str(output/result_name), "preview": str(output/"preview.jpg"),
                       "comparison": str(output/"comparison.jpg"), "recipe": str(output/"recipe.json")}
        recipe = {"schema_version": 1, "status": "ok", "input": str(source),
                  "input_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                  "request": request, "plan": plan, "preparation": prepared,
                  "engine": selected, "backend": backend, "fallback_attempts": attempts,
                  "postprocess": post, "warnings": warnings, "outputs": final_paths,
                  "qc": {"size": list(result.size), "constant_image": constant, "icc": "sRGB"},
                  "elapsed_s": time.perf_counter()-started,
                  "aesthetic_evaluation": "reserved for the user"}
        (bundle/"recipe.json").write_text(json.dumps(recipe, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        # Rename commits a complete delivery on the same filesystem. A racing
        # caller targeting the same output is rejected rather than merged.
        if output.exists():
            raise GradeError("OUTPUT_EXISTS", "Another operation created the requested output directory")
        bundle.rename(output)
    return {"status": "ok", "engine": selected, "preset": plan["preset"], "label": plan["label"], "selection": plan["selection"],
            "size": prepared["processing_size"], "outputs": final_paths, "warnings": warnings,
            "elapsed_s": recipe["elapsed_s"]}

def parser():
    ap = argparse.ArgumentParser(description=__doc__)
    commands = ap.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor")
    commands.add_parser("presets")
    commands.add_parser("menu")
    configure = commands.add_parser("configure"); configure.add_argument("--root", required=True)
    for name in ["plan", "grade", "psd"]:
        cmd = commands.add_parser(name)
        cmd.add_argument("--intent")
        cmd.add_argument("--preset", choices=list(PRESETS))
        cmd.add_argument("--effect", help="Menu ID 1-6, supported film name, or preset name")
        if name in ("grade", "psd"):
            cmd.add_argument("--input")
            cmd.add_argument("--output")
            cmd.add_argument("--request", type=Path)
            cmd.add_argument("--recipe", help="Previous recipe.json for an incremental revision")
            cmd.add_argument("--engine", choices=["auto", "spektralab", "spektrafilm"])
            cmd.add_argument("--max-edge", type=int)
            if name == "grade":
                cmd.add_argument("--format", choices=["jpg", "png"])
        if name == "psd":
            cmd.add_argument("--seed", type=int)
            cmd.add_argument("--width", type=int)
            cmd.add_argument("--height", type=int)
            cmd.add_argument("--resize-mode", choices=["keep_aspect", "stretch"])
            cmd.add_argument("--document-format", choices=["auto", "psd", "psb"])
            cmd.add_argument("--no-archive", action="store_true")
        for key in RANGES:
            cmd.add_argument("--"+key.replace("_", "-"), type=float)
        cmd.add_argument("--keep-exposure", action="store_true")
    return ap

def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = parser().parse_args()
    try:
        if args.command == "menu":
            emit(menu(SKILL/"references"/"user-guide.md"))
            return 0
        runtime = runtime_config()
        # Reuse the isolated environment already holding both render dependencies.
        # The argument vector is passed directly, without a shell.
        target = Path(interpreter(runtime)).resolve()
        if args.command in ("grade", "psd") and target != Path(sys.executable).resolve() and os.environ.get("SPEKTRA_BOOTSTRAPPED") != "1":
            environment = dict(os.environ, SPEKTRA_BOOTSTRAPPED="1", PYTHONIOENCODING="utf-8")
            return subprocess.run([str(target), str(Path(__file__).resolve()), *sys.argv[1:]], env=environment).returncode
        if args.command == "configure":
            configured = runtime_config(args.root)
            (SKILL/"runtime.local.json").write_text(json.dumps(configured, indent=2), encoding="utf-8")
            emit({"status": "configured", "runtime": configured})
        elif args.command == "doctor":
            result = doctor(runtime); emit(result)
            return 0 if result["status"] == "ready" else 2
        elif args.command == "presets":
            emit({"status": "ok", "presets": PRESETS, "ranges": RANGES})
        else:
            adjustments = {key: getattr(args, key) for key in RANGES if getattr(args, key) is not None}
            if args.keep_exposure:
                adjustments["auto_exposure"] = False
                adjustments["exposure_ev"] = 0.0
                adjustments["brightness_ev"] = 0.0
            if args.command == "plan":
                preset = effect_preset(args.effect, args.preset)
                plan = compile_preferences(args.intent or "", preset, adjustments)
                if args.effect is not None and plan["settings"]["film"] != PRESETS[preset]["film"]:
                    raise ValueError("Selected effect and film adjustment disagree")
                plan["selection"] = describe_selection(plan)
                emit({"status": "planned", **plan})
            else:
                flags = {key: value for key, value in {
                    "input": args.input, "output": args.output, "intent": args.intent, "effect": args.effect, "preset": args.preset,
                    "engine": args.engine, "max_edge": args.max_edge, "format": getattr(args, "format", None),
                    "recipe": args.recipe}.items() if value is not None}
                request = json.loads(args.request.read_text(encoding="utf-8-sig")) if args.request else {}
                if not isinstance(request, dict) or not isinstance(request.get("adjustments", {}), dict):
                    raise GradeError("INVALID_REQUEST", "Request and adjustments must be JSON objects")
                request.update(flags)
                request["adjustments"] = {**request.get("adjustments", {}), **adjustments}
                if args.command == "psd":
                    for key in ("width", "height", "resize_mode", "document_format"):
                        value = getattr(args, key)
                        if value is not None: request[key] = value
                    if args.no_archive: request["archive"] = False
                    if args.seed is not None:
                        request["seed"] = args.seed
                    from psd_pipeline import execute_psd_request
                    emit(execute_psd_request(request, runtime))
                else:
                    emit(execute_request(request, runtime))
        return 0
    except Exception as exc:
        code = getattr(exc, "code", "INVALID_PREFERENCES" if isinstance(exc, ValueError) else "EXECUTION_FAILED")
        emit({"status": "error", "code": code, "message": str(exc), "remedy": getattr(exc, "remedy", "")})
        return 2

if __name__ == "__main__":
    sys.modules.setdefault("spektra", sys.modules[__name__])
    raise SystemExit(main())
