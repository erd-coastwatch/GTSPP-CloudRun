#!/usr/bin/env bash
# Install the Cloud Run-generated subset JSON on erd-erddap; update the GTSPP history and request reload.
set -euo pipefail
[[ "$(id -u)" == 0 ]] || { echo "Run with sudo" >&2; exit 1; }
PROJECT_ID="${PROJECT_ID:-ggn-nmfs-swfscerddap-dev-1}"
BUCKET="${BUCKET:-erddap-swfsc-erd}"
CONTENT_DIR="${CONTENT_DIR:-/usr/local/tomcat_erddap/content/erddap}"
FLAG_DIR="${FLAG_DIR:-/mnt/disks/erddap-data/erddap/hardFlag}"
exec 9>/var/lock/gtspp-subset-install.lock
flock -n 9 || exit 0
temp_file="$(mktemp)"
temp_state="$(mktemp)"
trap 'unlink "$temp_file"; unlink "$temp_state"' EXIT
STATE_PREFIX="${STATE_PREFIX:-operations/workflow-state/gtspp/production}"
gcloud storage cp "gs://${BUCKET}/${STATE_PREFIX}/subset/erdGtsppBest.json" "$temp_file" --project "$PROJECT_ID"
gcloud storage cp "gs://${BUCKET}/${STATE_PREFIX}/state.json" "$temp_state" --project "$PROJECT_ID"
changed="$(python3 - "$temp_file" "${CONTENT_DIR}/subset/erdGtsppBest.json" "$temp_state" "${CONTENT_DIR}/datasets.xml" <<'PY'
import json, os, sys, tempfile, re, shutil, stat
from datetime import datetime, timezone
from pathlib import Path
source, target, state_path, xml_path = map(Path, sys.argv[1:])
payload = json.loads(source.read_text())
table = payload['table']
assert table['columnNames'] == ['trajectory','org','type','platform','cruise']
assert table['rows'] and all(len(row) == 5 for row in table['rows'])
state = json.loads(state_path.read_text())
if state.get('pending_months') or state.get('pending_subset'):
    raise SystemExit('Processing/subset refresh is incomplete; retry later')
export_date = datetime.fromisoformat(state['last_export_at']).astimezone(timezone.utc).strftime('%Y-%m-%d')
original = xml_path.read_bytes()
pattern = rb'(<dataset\b[^>]*\bdatasetID="erdGtsppBest"[^>]*>)(.*?)(</dataset>)'
matches = list(re.finditer(pattern, original, flags=re.S))
if len(matches) != 1: raise SystemExit('Expected exactly one erdGtsppBest definition')
match = matches[0]
block = match.group(2)
history_pattern = rb'(<att name="history">)(.*?)(</att>)'
histories = list(re.finditer(history_pattern, block, flags=re.S))
if len(histories) != 1: raise SystemExit('Expected exactly one GTSPP history attribute')
old_history = histories[0].group(2)
new_line = (export_date + ' Most recent ingest, clean, and reformat at ERD (erd.data at noaa.gov).').encode()
new_history, count = re.subn(rb'\d{4}-\d{2}-\d{2} Most recent ingest[^<\r\n]*',new_line,old_history)
if count != 1: raise SystemExit('Expected exactly one ingestion-history date')
updated_block = block[:histories[0].start(2)] + new_history + block[histories[0].end(2):]
updated_xml = original[:match.start(2)] + updated_block + original[match.end(2):]
changed = False
if not target.exists() or json.loads(target.read_text()) != payload:
    target.parent.mkdir(parents=True,exist_ok=True)
    fd,name = tempfile.mkstemp(prefix='.gtspp-subset-',dir=target.parent)
    try:
        with os.fdopen(fd,'w') as f: json.dump(payload,f); f.write('\n')
        os.chmod(name,0o644)
        os.replace(name,target)
    finally:
        if os.path.exists(name): os.unlink(name)
    changed = True
if updated_xml != original:
    # Only replace the one GTSPP history span; preserve all other XML bytes.
    import xml.etree.ElementTree as ET
    ET.fromstring(updated_xml)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    shutil.copy2(xml_path,str(xml_path)+'.gtspp-'+stamp+'.bak')
    file_stat=xml_path.stat()
    fd,name=tempfile.mkstemp(prefix='.gtspp-xml-',dir=xml_path.parent)
    try:
        with os.fdopen(fd,'wb') as f: f.write(updated_xml)
        os.chmod(name,stat.S_IMODE(file_stat.st_mode))
        os.chown(name,file_stat.st_uid,file_stat.st_gid)
        os.replace(name,xml_path)
    finally:
        if os.path.exists(name): os.unlink(name)
    changed=True
print('changed' if changed else 'unchanged')
PY
)"
if [[ "$changed" == unchanged ]]; then
  echo "GTSPP subset already current."
  exit 0
fi
touch "${FLAG_DIR}/erdGtsppBest"
echo "GTSPP subset/history updated and reload requested."
