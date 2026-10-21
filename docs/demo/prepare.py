"""Generate private synthetic configuration outside the clone; no service starts."""
import argparse
import re
import secrets
import shutil
import hashlib
import json
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--runtime", required=True)
parser.add_argument("--project", required=True)
args = parser.parse_args()
runtime = Path(args.runtime).resolve()
repo = Path(__file__).resolve().parents[2]
if runtime.drive.lower() != "f:" or runtime.exists() or runtime == repo or repo in runtime.parents:
    parser.error("Use a previously nonexistent external F: runtime directory")
if not re.fullmatch(r"interview-v2-demo-[a-z0-9-]+", args.project):
    parser.error("Use a new interview-v2-demo-* project name")
runtime.mkdir(parents=True)
backend = repo / "ai-interview-backend"
for name in ("app", "migrations", "tests"):
    shutil.copytree(backend / name, runtime / "backend" / name,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".env", "logs", "uploads"))
for name in ("main.py", "alembic.ini", "requirements.txt"):
    shutil.copyfile(backend / name, runtime / "backend" / name)
copied = {p.relative_to(runtime / "backend").as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
          for p in (runtime / "backend").rglob("*") if p.is_file()}
assert all(hashlib.sha256((backend / name).read_bytes()).hexdigest() == digest for name, digest in copied.items())
(runtime / "backend-source-hashes.json").write_text(json.dumps(copied, indent=2), encoding="utf-8")
env = dict(DEMO_PROJECT=args.project, DEMO_RUNTIME=runtime.as_posix(), DEMO_API_PORT="18096",
           DEMO_DB_PASSWORD=secrets.token_hex(16), DEMO_SIGNING_KEY=secrets.token_hex(32),
           DEMO_ACCOUNT_PASSWORD="Demo!"+secrets.token_hex(12))
(runtime / "demo.env").write_text("".join(f"{k}={v}\n" for k,v in env.items()), encoding="utf-8")
print(f"Created private synthetic configuration: {runtime / 'demo.env'}")
