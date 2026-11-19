"""Local SpektraLab host adapter. The official host owns film simulation."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import queue
import struct
import subprocess
import tempfile
import threading
import time


class BackendError(Exception):
    def __init__(self, code: str, message: str, stage: str):
        super().__init__(message)
        self.code, self.stage = code, stage


def _number(settings: dict, key: str, default: float, low: float, high: float) -> float:
    value = settings.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BackendError("invalid_settings", f"{key} must be a number", "validation")
    if not math.isfinite(value) or not low <= value <= high:
        raise BackendError("invalid_settings", f"{key} must be between {low} and {high}", "validation")
    return float(value)


def _parameters(settings: dict) -> dict:
    auto = settings.get("auto_exposure", True)
    if type(auto) is not bool:
        raise BackendError("invalid_settings", "auto_exposure must be a boolean", "validation")
    grain = _number(settings, "grain", 1, 0, 1)
    halation = _number(settings, "halation", 1, 0, 1)
    couplers = _number(settings, "dir_couplers", 1, 0, 1)
    params = {
        "film_stock": settings.get("film", "kodak_portra_400"),
        "print_stock": settings.get("paper", "kodak_portra_endura"),
        "exposure_compensation_ev": _number(settings, "exposure_ev", 0, -8, 8),
        "print_exposure": _number(settings, "print_exposure", 1, 0.05, 20),
        "auto_exposure": auto,
        "grain_active": grain > 0,
        "grain_amount": grain,
        "halation_active": halation > 0,
        "halation_amount": halation,
        "halation_scatter_amount": halation,
        "dir_couplers_active": couplers > 0,
        "dir_couplers_amount": couplers,
        "contrast_mask_active": False,
    }
    for name in ("film_stock", "print_stock"):
        if not isinstance(params[name], str) or not params[name].strip():
            raise BackendError("invalid_settings", f"{name} must be a stock ID", "validation")
    return params


class _Host:
    def __init__(self, executable: Path, resources: Path, deadline: float):
        self.deadline = deadline
        self.responses = queue.Queue()
        self.sequence = 0
        self.timings = {}
        self.log = tempfile.TemporaryFile()
        try:
            self.process = subprocess.Popen(
                [str(executable), "--resources", str(resources)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log,
                cwd=str(executable.parent),
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except Exception:
            self.log.close()
            raise
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _exact(self, size: int) -> bytes:
        result = bytearray()
        while len(result) < size:
            block = self.process.stdout.read(size - len(result))
            if not block:
                raise EOFError(f"Host exited with code {self.process.poll()}")
            result.extend(block)
        return bytes(result)

    def _read(self):
        try:
            while True:
                header_size, payload_size = struct.unpack("<II", self._exact(8))
                if header_size > 64 * 1024 * 1024 or payload_size:
                    raise ValueError("Unexpected host frame size or pixel payload")
                header = json.loads(self._exact(header_size))
                if "id" in header:
                    self.responses.put(header)
        except Exception as error:
            self.responses.put(error)

    def _send(self, method: str, params: dict) -> int:
        self.sequence += 1
        body = json.dumps({"id": self.sequence, "method": method, "params": params},
                          ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.process.stdin.write(struct.pack("<II", len(body), 0) + body)
        self.process.stdin.flush()
        return self.sequence

    def call(self, method: str, params: dict | None = None) -> dict:
        started = time.perf_counter()
        try:
            if time.monotonic() >= self.deadline:
                raise BackendError("timeout", "SpektraLab render deadline exceeded", method)
            request_id = self._send(method, params or {})
            while True:
                remaining = self.deadline - time.monotonic()
                if remaining <= 0:
                    raise queue.Empty
                response = self.responses.get(timeout=remaining)
                if isinstance(response, Exception):
                    raise BackendError("protocol_error", str(response), method)
                if response.get("id") != request_id:
                    continue
                if not response.get("ok"):
                    error = response.get("error", {})
                    raise BackendError(error.get("code", "host_error"),
                                       error.get("message", "Host request failed"), method)
                return response["result"]
        except queue.Empty as error:
            raise BackendError("timeout", "SpektraLab render deadline exceeded", method) from error
        finally:
            self.timings[method] = time.perf_counter() - started

    def close(self) -> str:
        diagnostics = []
        try:
            if self.process.poll() is None:
                try:
                    self._send("shutdown", {})
                except (OSError, ValueError):
                    pass
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=2)
        except Exception as error:
            diagnostics.append(f"Host cleanup: {error}")
        finally:
            self.reader.join(timeout=1)
            for stream in (self.process.stdin, self.process.stdout):
                try:
                    stream.close()
                except OSError:
                    pass
        try:
            self.log.seek(0)
            text = self.log.read().decode("utf-8", errors="replace")[-6000:]
        finally:
            self.log.close()
        return text + "\n".join(diagnostics)


def render(input_path: Path, output_path: Path, settings: dict, runtime: dict) -> dict:
    """Return JSON-safe result metadata, including structured errors and timings.

    runtime requires lab_host and lab_resources; timeout_s defaults to 180.
    Existing outputs are refused. PNG is sRGB8; JPEG uses quality 90.
    """
    started = time.perf_counter()
    result = {"ok": False, "backend": "spektralab"}
    host = None
    stage = "validation"
    try:
        from PIL import Image

        source, target = Path(input_path).resolve(), Path(output_path).resolve()
        executable = Path(runtime["lab_host"]).resolve()
        resources = Path(runtime["lab_resources"]).resolve()
        timeout = float(runtime.get("timeout_s", 180))
        if not math.isfinite(timeout) or timeout <= 0:
            raise BackendError("invalid_runtime", "timeout_s must be positive and finite", stage)
        if not source.is_file():
            raise BackendError("input_missing", f"Input file does not exist: {source}", stage)
        if source == target or target.exists():
            raise BackendError("output_exists", f"Output must be a new file: {target}", stage)
        formats = {".png": ("png", "PNG"), ".jpg": ("jpeg", "JPEG"), ".jpeg": ("jpeg", "JPEG")}
        if target.suffix.lower() not in formats:
            raise BackendError("invalid_output", "Output extension must be .png, .jpg or .jpeg", stage)
        if not executable.is_file() or not resources.is_dir():
            raise BackendError("runtime_missing", "SpektraLab host or engine resources are missing", stage)
        params = _parameters(settings)
        target.parent.mkdir(parents=True, exist_ok=True)
        stage = "startup"
        host = _Host(executable, resources, time.monotonic() + timeout)
        hello = host.call("hello")
        result.update(host_version=hello.get("host_version"), host_backend=hello.get("backend"))
        if hello.get("protocol") != 1:
            raise BackendError("protocol_error", "SpektraLab host protocol 1 is required", "hello")
        if not hello.get("backend", {}).get("available"):
            raise BackendError("backend_unavailable", hello.get("backend", {}).get("error", "GPU unavailable"), "hello")
        stage = "open"
        opened = host.call("open", {"path": str(source), "params": params})
        session = opened["session"]
        result["open_timings_ms"] = opened.get("timings_ms", {})
        # Automatic exposure is applied by the engine. A solve result must not
        # be reapplied as manual compensation, which would double the gain.
        resolved = host.call("get_params", {"session": session})
        result["params"] = resolved["params"]
        stage = "export_image"
        exported = host.call("export_image", {"session": session, "path": str(target),
            "format": formats[target.suffix.lower()][0], "quality": 90,
            "color_space": "sRGB", "overwrite": False})
        host.call("close", {"session": session})
        stage = "validate_output"
        with Image.open(target) as image:
            image.load()
            if image.format != formats[target.suffix.lower()][1] or not image.info.get("icc_profile"):
                raise BackendError("invalid_render", "Output format or ICC profile is missing", stage)
            if image.size != (opened["width"], opened["height"]):
                raise BackendError("invalid_render", "Output dimensions differ from decoded input", stage)
            result.update(width=image.width, height=image.height, mode=image.mode, icc_profile=True)
        result.update(ok=True, output_path=str(target), export=exported)
    except BackendError as error:
        result["error"] = {"code": error.code, "message": str(error), "stage": error.stage}
    except ImportError as error:
        result["error"] = {"code": "dependency_missing", "message": str(error), "stage": stage}
    except Exception as error:
        result["error"] = {"code": "backend_error", "message": str(error), "stage": stage}
    finally:
        if host is not None:
            result["api_timings_s"] = host.timings
            result["host_log"] = host.close()
        result["elapsed_s"] = time.perf_counter() - started
    return result
