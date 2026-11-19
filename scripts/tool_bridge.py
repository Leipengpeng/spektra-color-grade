"""JSON stdin bridge for a host executing an Agent's function call locally."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile

def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        request = json.loads(sys.stdin.buffer.read().decode("utf-8-sig"))
        if not isinstance(request, dict):
            raise ValueError("Expected a tool call or grading request object")
        command = "grade"
        if "name" in request:
            if request["name"] not in {"spektra_grade", "spektra_menu", "spektra_psd"}:
                raise ValueError("Unknown tool name")
            command = {"spektra_menu": "menu", "spektra_grade": "grade", "spektra_psd": "psd"}[request["name"]]
            arguments = request["arguments"]
            request = json.loads(arguments) if isinstance(arguments, str) else arguments
        if not isinstance(request, dict):
            raise ValueError("Expected a grading request object")
        if command == "menu" and request:
            raise ValueError("spektra_menu takes an empty arguments object")
        with tempfile.TemporaryDirectory(prefix="spektra-tool-") as folder:
            path = Path(folder)/"request.json"
            path.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
            script = Path(__file__).with_name("spektra.py")
            arguments = [command, "--request", str(path)] if command != "menu" else ["menu"]
            result = subprocess.run([sys.executable, str(script), *arguments],
                                    capture_output=True, encoding="utf-8", errors="replace")
        if result.stdout.strip():
            value = json.loads(result.stdout)
        else:
            value = {"status": "error", "code": "BRIDGE_FAILED", "message": result.stderr[-2000:]}
        print(json.dumps(value, ensure_ascii=False, allow_nan=False))
        return result.returncode
    except Exception as exc:
        print(json.dumps({"status": "error", "code": "INVALID_TOOL_REQUEST", "message": str(exc)}, ensure_ascii=False))
        return 2

if __name__ == "__main__":
    raise SystemExit(main())
