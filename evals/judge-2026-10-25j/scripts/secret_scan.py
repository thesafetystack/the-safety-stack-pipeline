"""Secret scan for judge outputs before anything derived from them is copied into the repository.

    secret_scan.py PATH [PATH ...]

Scans files (directories recursively) for: gitleaks findings (when ``gitleaks`` is on PATH; it is skipped with a
notice otherwise), credential patterns (API keys, tokens, private keys, auth-file fields, bearer headers), the
private bucket name (read in-process from EVAL104_BLOBS_GLOB's resolution, never printed) and home / tmp paths.
Prints ``file:line rule`` per finding (never the matched text) and exits 1 when anything is found.
The judges' raw outputs are expected to hit the path and text rules (they echo documents); scan what you intend to
copy: labels, score files, items.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C  # noqa: E402

RULES = {
    "anthropic_key": r"sk-ant-[A-Za-z0-9_-]{20,}",
    "openai_key": r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{20,}",
    "openrouter_key": r"sk-or-v1-[A-Za-z0-9]{20,}",
    "aws_access_key": r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b",
    "aws_secret": r"(?i)aws_secret_access_key\s*[=:]\s*\S{20,}",
    "github_token": r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}",
    "slack_token": r"\bxox[abprs]-[A-Za-z0-9-]{10,}",
    "google_key": r"\bAIza[0-9A-Za-z_-]{35}\b",
    "private_key": r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY",
    "jwt": r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}",
    "bearer": r"(?i)authorization:\s*bearer\s+[A-Za-z0-9._-]{20,}",
    "auth_file_field": r'"(?:access_token|refresh_token|id_token|OPENAI_API_KEY|api_key|accessToken|refreshToken)"\s*:\s*"[^"]{12,}"',
    "generic_secret_assign": r"(?i)\b(?:api[_-]?key|secret[_-]?key|password|passwd|token)\s*[=:]\s*['\"][^'\"\s]{16,}['\"]",
    "home_path": r"/home/[A-Za-z0-9._-]+/",
    "tmp_path": r"(?<![A-Za-z0-9])/t" + r"mp/",  # split so this file does not match itself
}


def files_of(paths: list[str]) -> list[Path]:
    out = []
    for p in map(Path, paths):
        out += sorted(x for x in p.rglob("*") if x.is_file() and "__pycache__" not in x.parts) if p.is_dir() else [p]
    return out


def gitleaks(paths: list[str]) -> int:
    exe = shutil.which("gitleaks")
    if not exe:
        print("notice: gitleaks not installed; regex rules only")
        return 0
    n = 0
    for p in paths:
        with tempfile.NamedTemporaryFile(suffix=".json") as rep:
            subprocess.run([exe, "detect", "--no-git", "--redact", "-s", p, "-r", rep.name, "--exit-code", "0"],
                           capture_output=True, text=True)
            try:
                found = json.loads(Path(rep.name).read_text() or "[]")
            except json.JSONDecodeError:
                found = []
            for f in found:
                print(f"{f.get('File')}:{f.get('StartLine')} gitleaks:{f.get('RuleID')}")
                n += 1
    return n


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    rules = {k: re.compile(v) for k, v in RULES.items()}
    names = C.Blobs().secret_names()
    rules.update({f"bucket_name_{i}": re.compile(re.escape(n)) for i, n in enumerate(names)})
    n = gitleaks(argv)
    for f in files_of(argv):
        try:
            text = f.read_text("utf-8", errors="replace")
        except OSError as exc:
            print(f"{f}: unreadable ({exc.__class__.__name__})")
            n += 1
            continue
        for ln, line in enumerate(text.splitlines(), 1):
            for name, rx in rules.items():
                if rx.search(line):
                    print(f"{f}:{ln} {name}")
                    n += 1
    print(f"{n} finding(s) in {len(files_of(argv))} file(s); bucket-name rules: {len(names)}")
    return 1 if n else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
