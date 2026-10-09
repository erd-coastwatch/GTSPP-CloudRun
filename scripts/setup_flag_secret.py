#!/usr/bin/env python3
"""Store the GTSPP dataset flag key in regional Secret Manager, without echoing it."""
import getpass
import os
import re
import subprocess

project = os.environ.get('PROJECT_ID', 'ggn-nmfs-swfscerddap-dev-1')
region = os.environ.get('REGION', 'us-east4')
secret = os.environ.get('FLAG_SECRET', 'erddap-flag-erd-gtspp-best')
account = os.environ.get('SERVICE_ACCOUNT', f'swfscerddap-run-sa1@{project}.iam.gserviceaccount.com')
key = getpass.getpass('Paste the erdGtsppBest flag key (hidden): ').strip()
if not re.fullmatch(r'[0-9a-fA-F]{64}', key):
    raise SystemExit('Expected the dataset-specific 64-character hexadecimal flag key.')
args = ['--location', region, '--project', project]
result = subprocess.run(['gcloud', 'secrets', 'describe', secret, *args], capture_output=True, text=True)
if result.returncode:
    if 'NOT_FOUND' not in result.stderr:
        raise SystemExit(result.stderr)
    subprocess.run(['gcloud', 'secrets', 'create', secret, *args], check=True)
subprocess.run(['gcloud', 'secrets', 'versions', 'add', secret, *args, '--data-file=-'], input=key, text=True, check=True)
subprocess.run(['gcloud', 'secrets', 'add-iam-policy-binding', secret, *args,
                '--member', f'serviceAccount:{account}', '--role', 'roles/secretmanager.secretAccessor'], check=True)
print('GTSPP flag secret ready.')
