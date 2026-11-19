"""Build a portable Skill ZIP from an explicit source allowlist."""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import re
import zipfile

TOP_FILES = ('SKILL.md', 'README.md', 'LICENSE', 'NOTICE.md', 'VERSION')
DIRECTORIES = ('agents', 'references', 'scripts')
TEXT_SUFFIXES = {'.md', '.py', '.txt', '.json', '.yaml', '.yml'}
SENSITIVE_PATTERNS = (
    re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b'),
    re.compile(r'\bAKIA[A-Z0-9]{16}\b'),
    re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
)


def package_files(root: Path) -> list[Path]:
    root = root.resolve()
    files = [root / name for name in TOP_FILES]
    for name in DIRECTORIES:
        directory = root / name
        if not directory.is_dir():
            raise ValueError(f'Missing package directory: {name}')
        for path in sorted(directory.rglob('*')):
            if '__pycache__' in path.parts or path.suffix in ('.pyc', '.pyo'):
                continue
            if path.is_symlink():
                raise ValueError(f'Symbolic links are not release inputs: {path.relative_to(root)}')
            if path.is_file():
                if path.name in ('runtime.local.json', '.env') or path.suffix.lower() not in TEXT_SUFFIXES:
                    raise ValueError(f'Unexpected package file: {path.relative_to(root)}')
                files.append(path)
    for path in files:
        if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError(f'Invalid release input: {path}')
        content = path.read_text(encoding='utf-8-sig')
        if any(pattern.search(content) for pattern in SENSITIVE_PATTERNS):
            raise ValueError(f'Potential credential in release input: {path.relative_to(root)}')
    return sorted(files)


def build(root: Path, output: Path) -> dict:
    root = root.resolve()
    files = package_files(root)
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    # An existing release is evidence; choose a new output rather than replacing it.
    with zipfile.ZipFile(output, 'x', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in files:
            archive.write(path, 'spektra-color-grade/' + path.relative_to(root).as_posix())
    with zipfile.ZipFile(output) as archive:
        if archive.testzip() is not None:
            raise ValueError('ZIP CRC validation failed')
        if 'spektra-color-grade/SKILL.md' not in archive.namelist():
            raise ValueError('Missing Skill entrypoint')
    return {'path': str(output), 'files': len(files), 'bytes': output.stat().st_size,
            'sha256': hashlib.sha256(output.read_bytes()).hexdigest()}


if __name__ == '__main__':
    import json
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(build(args.root, args.output), ensure_ascii=False))
