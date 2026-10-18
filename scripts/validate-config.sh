#!/usr/bin/env bash
# Validate the configuration without changing anything; safe while the service runs:
#   - Nginx's units from .env through Podman's Quadlet generator, and `nginx -t` with the current
#     generated user map in a throwaway container (scripts/install-units.py --check);
#   - the installed per-user units (QUADLET_DIR) through the generator as well.
# (Authelia is validated by its own deployment: authelia validate-config.)
# Exits non-zero on the first failure.
#
# Usage: sudo ./scripts/validate-config.sh
set -euo pipefail

repo_dir="$(cd "$(dirname "$0")/.." && pwd)"
env_file="${ENV_FILE:-$repo_dir/.env}"

"$repo_dir/scripts/install-units.py" --env-file "$env_file" --check

echo "== installed user units"
python3 - "$repo_dir" "$env_file" <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, f"{sys.argv[1]}/scripts")
import rb_units
from freeipa_common import read_env_file
qdir = Path(read_env_file(Path(sys.argv[2])).get("QUADLET_DIR") or rb_units.QUADLET_DIR)
units = {p.name: p.read_text() for p in sorted(qdir.glob("*")) if p.suffix in (".container", ".network")}
if not units:
    print(f"none in {qdir} (first sync not run yet)")
    sys.exit(0)
try:
    rb_units.validate(units)
except rb_units.UnitError as exc:
    sys.exit(f"rejected: {exc}")
print(f"{len(units)} unit(s) in {qdir} ok")
PY

echo "OK"
