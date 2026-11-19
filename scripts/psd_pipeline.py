"""Fixed high-precision render, difference-layer PSD, validation and delivery flow."""
from __future__ import annotations
import contextlib
import hashlib
import io
import json
from pathlib import Path
import tempfile
import time
import zipfile

import numpy as np
from PIL import Image, ImageCms, ImageOps

from catalogue import describe_selection, effect_preset
from preferences import PRESETS, compile_preferences, detect_style

class WorkflowError(Exception):
    def __init__(self, code, message, remedy=""):
        super().__init__(message)
        self.code, self.remedy = code, remedy

def _decode(x):
    return np.where(x <= .04045, x/12.92, ((x+.055)/1.055)**2.4)

def _encode(x):
    return np.where(x <= .0031308, x*12.92, 1.055*np.maximum(x, 0)**(1/2.4)-.055)

def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024*1024), b""):
            digest.update(block)
    return digest.hexdigest()

def postprocess(original, rendered, settings):
    linear = _decode(rendered.astype(np.float64))
    w, t = settings["warmth"], settings["tint"]
    gain = np.exp2([.30*w+.10*t, -.10*t, -.30*w+.10*t])*2**settings["brightness_ev"]
    linear *= gain
    gain_clip = float(((linear < 0) | (linear > 1)).mean())
    x = _encode(np.clip(linear, 0, 1))
    luminance = x @ np.array([.2126, .7152, .0722])
    x = luminance[..., None]+settings["saturation"]*(x-luminance[..., None])
    x = (x-.5)*settings["contrast"]+.5
    x = x*(1-settings["fade"])+settings["fade"]
    x = original*(1-settings["strength"])+x*settings["strength"]
    clipping = float(((x < 0) | (x > 1)).mean())
    return np.clip(x, 0, 1).astype(np.float32), {"linear_gain_clip_fraction": gain_clip, "post_clip_channel_fraction": clipping}

def _small_image(x):
    step = max(1, int(np.ceil(max(x.shape[:2])/1600)))
    return Image.fromarray(np.rint(np.clip(x[::step,::step], 0, 1)*255).astype("uint8"))

def make_delivery_zip(folder):
    folder = Path(folder)
    archive = folder/"delivery.zip"
    names = {"graded.psd", "graded.psb", "graded.tif", "preview.jpg", "comparison.jpg", "recipe.json", "layers.json", "validation.json", "PS使用说明.md"}
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as package:
        for path in sorted(folder.iterdir()):
            if path.is_file() and path.name in names:
                package.write(path, path.name)
    with zipfile.ZipFile(archive) as package:
        if package.testzip() is not None:
            raise WorkflowError("ARCHIVE_FAILED", "Delivery archive integrity check failed")
    return archive

def _execute_psd_request(request, runtime):
    # Import helpers without starting the CLI. The caller can also import this API.
    from spektra import validate_request, backend_available, ENGINE_KEYS, compare_sheet
    if not isinstance(request, dict):
        raise WorkflowError("INVALID_REQUEST", "Expected a JSON object")
    request = dict(request)
    if "format" in request:
        raise WorkflowError("INVALID_REQUEST", "PSD delivery is fixed to PSD16 plus TIFF16 and JPEG previews; format belongs to the legacy grade command")
    explicit_input = bool(request.get("input"))
    previous = None
    if request.get("recipe"):
        previous = json.loads(Path(request["recipe"]).expanduser().read_text(encoding="utf-8-sig"))
        if previous.get("schema_version") not in (1, 2) or not isinstance(previous.get("plan", {}).get("settings"), dict):
            raise WorkflowError("INVALID_RECIPE", "Use a recipe.json produced by this Skill")
        previous_request = previous["request"]
        if not any(key in request for key in ("max_edge", "width", "height")):
            old_edge = previous_request.get("max_edge", max(previous.get("preparation", {}).get("processing_size", [1600])) if previous.get("preparation", {}).get("resized") else 0)
            request.setdefault("max_edge", old_edge)
            for key in ("width", "height", "resize_mode"):
                if key in previous_request: request.setdefault(key, previous_request[key])
        for key, value in {"input": previous["input"], "engine": previous["engine"],
                           "seed": previous_request.get("seed", 20261112)}.items():
            request.setdefault(key, value)
        for key in ("document_format", "archive"):
            if key in previous_request: request.setdefault(key, previous_request[key])
    request.setdefault("max_edge", 0)
    seed = request.get("seed", 20261112)
    if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed <= 2**32-1:
        raise WorkflowError("INVALID_REQUEST", "seed must be an integer in [0,4294967295]")
    extra = {"seed", "width", "height", "resize_mode", "document_format", "archive"}
    if request.get("document_format", "auto") not in ("auto", "psd", "psb"):
        raise WorkflowError("INVALID_REQUEST", "document_format must be auto, psd or psb")
    if not isinstance(request.get("archive", True), bool):
        raise WorkflowError("INVALID_REQUEST", "archive must be a boolean")
    check = {k: v for k, v in request.items() if k not in extra}
    # PSD geometry has its own PSB-aware validation; legacy grade remains capped.
    edge = request.get("max_edge", 0)
    if isinstance(edge, bool) or not isinstance(edge, int) or not 0 <= edge <= 300000:
        raise WorkflowError("INVALID_REQUEST", "max_edge must be 0 (original) or integer pixels up to 300000")
    check["max_edge"] = 1600
    source, output = validate_request(check)
    chosen = effect_preset(request.get("effect"), request.get("preset"))
    detected = detect_style(request.get("intent", ""))
    new_preset = chosen or (detected if previous and detected != previous["plan"]["preset"] else None)
    inherited = previous["plan"]["settings"] if previous and not new_preset else None
    plan = compile_preferences(request.get("intent", ""), new_preset or (previous["plan"]["preset"] if inherited else None), request.get("adjustments"), inherited)
    if request.get("effect") is not None and plan["settings"]["film"] != PRESETS[chosen]["film"]:
        raise WorkflowError("INVALID_PREFERENCES", "Selected effect and adjustments.film disagree")
    plan["selection"] = describe_selection(plan)
    request.setdefault("seed", seed)
    engine = request.get("engine", "auto")
    candidates = [n for n in (["spektralab", "spektrafilm"] if engine == "auto" else [engine]) if backend_available(n, runtime)]
    if not candidates:
        raise WorkflowError("RUNTIME_MISSING", "No configured engine is available", "Run doctor and configure the local runtime.")
    started = time.perf_counter()
    original_hash = _sha256(source)
    if previous and not explicit_input and previous.get("input_sha256") not in (None, original_hash):
        raise WorkflowError("SOURCE_CHANGED", "The original file no longer matches this recipe", "Provide the intended new input explicitly, or restore the recipe's original file.")
    from disk_frames import prepare, mapped, rows
    work = Path(runtime["work_dir"])
    registry = runtime["_memmaps"]
    original, preparation = prepare(source, work, registry, edge, request.get("width"), request.get("height"), request.get("resize_mode", "keep_aspect"))
    from psd_writer import choose_document_format
    if request.get("document_format") == "psd" and choose_document_format(original.shape[1],original.shape[0],5) == "psb":
        raise WorkflowError("PSD_LIMIT", "This uncompressed layered document requires PSB", "Use document_format=auto or psb; the requested dimensions were not reduced.")
    output.parent.mkdir(parents=True, exist_ok=True)
    errors = []
    core = {key: plan["settings"][key] for key in ENGINE_KEYS}; core["seed"] = seed
    linear = mapped(work, "linear-input", original.shape, registry)
    for part in rows(original.shape[0], original.shape[1]):linear[part] = _decode(original[part])
    linear.flush()
    runtime.setdefault("timeout_s", max(300, min(7200, int(original.shape[0]*original.shape[1]/10000)+120)))
    for selected in candidates:
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                if selected == "spektralab":
                    from lab_passes import render_passes
                else:
                    from render_passes import render_passes
                result = render_passes(linear, core, runtime)
            stages = result["stages"]
            if [s["key"] for s in stages] != ["base", "exposure", "halation", "dir", "grain"]:
                raise ValueError("Renderer returned an incomplete stage sequence")
            for stage in stages:
                a = stage["rgb"]
                if a.shape != original.shape:
                    raise ValueError("Renderer returned invalid stage pixels")
                for part in rows(a.shape[0], a.shape[1]):
                    if not np.isfinite(a[part]).all() or np.any(a[part] < 0) or np.any(a[part] > 1):
                        raise ValueError("Renderer returned invalid stage pixels")
            break
        except Exception as exc:
            message = str(exc)
            resource_error = isinstance(exc,MemoryError) or any(word in message.lower() for word in ("resource_limit", "insufficient system commit", "out of memory", "available memory", "exceeds available", "storagebuffer", "maxstoragebuffer", "memory budget"))
            errors.append({"engine": selected, "code":"RESOURCE_LIMIT" if resource_error else getattr(exc,"code","RENDER_FAILED"), "message":message})
    else:
        if errors and all(e["code"]=="RESOURCE_LIMIT" for e in errors):
            raise WorkflowError("RESOURCE_LIMIT",json.dumps(errors,ensure_ascii=False),"Free RAM/commit/GPU resources or choose another machine; retry only after resources change. No dimensions were reduced and no partial document was delivered.")
        raise WorkflowError("RENDER_FAILED", json.dumps(errors, ensure_ascii=False), "No partial PSD was delivered. Explicit engines never switch to another engine.")
    # Apply the same global transform/strength to every physical stage before
    # differencing. A top-only compensation would cancel part of the grain and
    # make reducing grain opacity add inverse grain when strength is low.
    conditioned = []
    for stage in stages:
        frame = mapped(work, "conditioned-"+stage["key"], original.shape, registry)
        stats_total = {"linear_gain_clip_fraction": 0.0, "post_clip_channel_fraction": 0.0}
        for part in rows(original.shape[0],original.shape[1]):
            frame[part], stats = postprocess(original[part],stage["rgb"][part],plan["settings"])
            for key in stats_total:stats_total[key] += stats[key]*(part.stop-part.start)/original.shape[0]
        frame.flush()
        conditioned.append({**stage,"rgb":frame,"metadata":{**stage.get("metadata",{}),"global_post":stats_total}})
    final = conditioned[-1]["rgb"]
    stages = [stages[0], {"key":"post","name":"全局冷暖与力度（基础条件差分）",
                          "rgb":conditioned[0]["rgb"],"metadata":conditioned[0]["metadata"]}, *conditioned[1:]]
    if _sha256(source) != original_hash:
        raise WorkflowError("SOURCE_CHANGED", "Source file changed during rendering; delivery aborted")
    from psd_writer import write_layered_psd, choose_document_format
    from psd_validate import validate_psd
    import tifffile
    profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    warnings = preparation["warnings"]+plan["warnings"]+result.get("warnings", [])
    with tempfile.TemporaryDirectory(prefix=".spektra-psd-", dir=output.parent) as folder:
        delivery = Path(folder)/"delivery"; delivery.mkdir()
        provenance = {"engine": selected, "seed": seed, "input_sha256": original_hash, "plan": plan,
                      "renderer": result.get("metadata"), "source_bits": preparation["input_bits"]}
        document_format = request.get("document_format", "auto")
        suffix = choose_document_format(original.shape[1], original.shape[0], len(stages)-1) if document_format == "auto" else document_format
        document_name = "graded."+suffix
        psd_info = write_layered_psd(delivery/document_name, original, stages[0]["rgb"], stages[1:], profile, provenance, document_format=document_format)
        document_name = Path(psd_info["path"]).name
        validation = validate_psd(delivery/document_name, reference_rgb=final)
        if validation.get("status") not in ("ok", "passed"):
            raise WorkflowError("PSD_VALIDATION_FAILED", json.dumps(validation, ensure_ascii=False))
        pixels16 = tifffile.memmap(delivery/"graded.tif", shape=final.shape, dtype=np.uint16,
                         photometric="rgb", metadata=None, bigtiff=final.size*2 > 4_000_000_000,
                         extratags=[(34675, "B", len(profile), profile, False)])
        try:
            for part in rows(final.shape[0], final.shape[1]):pixels16[part] = np.rint(final[part].astype(np.float64)*65535).astype(np.uint16)
            pixels16.flush()
        finally:pixels16._mmap.close()
        preview = _small_image(final); preview.thumbnail((960, 960))
        preview.save(delivery/"preview.jpg", quality=82, optimize=True, icc_profile=profile)
        compare_sheet(_small_image(original), _small_image(final), delivery/"comparison.jpg", profile)
        names = {"psd": document_name, "image": "graded.tif", "preview": "preview.jpg", "comparison": "comparison.jpg",
                 "recipe": "recipe.json", "layers": "layers.json", "validation": "validation.json", "instructions": "PS使用说明.md"}
        if request.get("archive", True):names["archive"] = "delivery.zip"
        paths = {key: str(output/name) for key, name in names.items()}
        psd_info["path"] = paths["psd"]
        validation["path"] = paths["psd"]
        layer_manifest = {"schema_version": 1, "psd": psd_info, "stages": [{k: v for k, v in s.items() if k != "rgb"} for s in stages],
                          "semantics": "Conditional differences for this source and recipe, not independently recomputed physical effects"}
        recipe = {"schema_version": 2, "delivery": "layered_psd", "status": "ok", "input": str(source), "input_sha256": original_hash,
                  "request": request, "plan": plan, "engine": selected, "preparation": preparation,
                  "backend": result.get("metadata"), "fallback_attempts": errors, "warnings": warnings,
                  "outputs": paths, "psd": psd_info, "validation": validation, "elapsed_s": time.perf_counter()-started,
                  "aesthetic_evaluation": "reserved for user"}
        for name, data in [("recipe.json", recipe), ("layers.json", layer_manifest), ("validation.json", validation)]:
            (delivery/name).write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        (delivery/"PS使用说明.md").write_text("# Spektra 分层成片\n\n打开本目录的 graded.psd 或 graded.psb（大图自动使用PSB）。底部可见原片是色彩管理及可选缩放后的底图；降低‘基础调色’不透明度可露出它，源文件未改。基础调色之后是全局基础调整条件差分，随后为曝光、光晕、DIR、颗粒；全局参数已一致应用于各阶段，真正修改全图冷暖和力度需用配方重算，顶层‘后续修饰’留给你修图。\n\n初始默认状态用于复现这次成片。效果层默认不透明度按像素余量自动设置，勿一律改成100%。可降低不透明度减弱，未达100%的层可继续上调；已经100%的层可复制增强（可能剪切），或让 Agent 按更高物理参数重算。每层实际默认值和增强余量见 layers.json。可用蒙版做局部创作修改。\n\n任意关层、复制、重排或改底图不会让原胶片算法重新计算。要改变胶片或物理效果参数，请让 Agent 使用本目录 recipe.json 生成新结果。差分层使用线性光（Linear Light）混合，接近灰色是差值编码，请保留其混合模式。按原编码 sRGB 混合；改变混合Gamma或色彩空间后需比较参考图。graded.tif 是独立16位参考，preview/comparison 是压缩评审图。\n\nvalidation.json 记录实际验证器：小图使用开源 psd-tools 强制图层合成，大图使用独立流式 PSD/PSB 解析合成。它们验证默认重建及图层响应，不是 Adobe Photoshop 实机认证，也不是美学评分。\n", encoding="utf-8")
        if request.get("archive", True):make_delivery_zip(delivery)
        if output.exists():
            raise WorkflowError("OUTPUT_EXISTS", "Output appeared during rendering; choose a new directory")
        delivery.rename(output)
    return {"status": "ok", "delivery": "layered_psd", "engine": selected, "selection": plan["selection"],
            "size": preparation["processing_size"], "document_format": psd_info["format"], "depth": 16, "layers": psd_info["layer_count"],
            "outputs": paths, "validation": validation, "warnings": warnings, "elapsed_s": recipe["elapsed_s"]}

def execute_psd_request(request, runtime):
    if not isinstance(request, dict) or not isinstance(request.get("output"),str) or not request["output"].strip():
        raise WorkflowError("INVALID_REQUEST", "A new output directory is required")
    output=Path(request["output"]).expanduser().resolve()
    if output.exists():raise WorkflowError("OUTPUT_EXISTS", "Choose a new output directory")
    output.parent.mkdir(parents=True,exist_ok=True)
    maps=[]
    with tempfile.TemporaryDirectory(prefix=".spektra-work-",dir=output.parent) as folder:
        configured=dict(runtime, work_dir=folder, _memmaps=maps)
        try:return _execute_psd_request(request,configured)
        finally:
            seen=set()
            for frame in reversed(maps):
                mm=getattr(frame,"_mmap",None)
                if mm is not None and id(mm) not in seen:
                    seen.add(id(mm))
                    try:frame.flush();mm.close()
                    except (ValueError,OSError):pass
