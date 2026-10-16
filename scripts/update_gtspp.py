#!/usr/bin/env python3
"""Monthly GTSPP ingestion with revision tracking, staging, and safe publication."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import tempfile
import time
from urllib.parse import urljoin
import uuid

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from gtspp_core import ProfileStore, month_bounds, write_cf

LOG = logging.getLogger("gtspp")
UTC = timezone.utc
BASINS = ("at", "in", "pa")
SOURCE_URL = "https://www.ncei.noaa.gov/data/oceans/gtspp/bestcopy/netcdf/"


class ListingParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.cells = None
        self.cell = None
        self.href = None
        self.entries = {}

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.cells, self.href = [], None
        elif tag == "td" and self.cells is not None:
            self.cell = ""
        elif tag == "a" and self.cells is not None:
            href = dict(attrs).get("href", "")
            if re.fullmatch(r"gtspp4_(at|in|pa)\d{6}\.tgz", href):
                self.href = href

    def handle_data(self, data):
        if self.cell is not None:
            self.cell += data

    def handle_endtag(self, tag):
        if tag == "td" and self.cell is not None:
            self.cells.append(self.cell.strip())
            self.cell = None
        elif tag == "tr" and self.cells is not None:
            if self.href:
                dates = [s for s in self.cells if re.fullmatch(r"\d{4}-\d\d-\d\d \d\d:\d\d", s)]
                if len(dates) != 1:
                    raise ValueError(f"Missing revision timestamp for {self.href}")
                self.entries[self.href] = dates[0]
            self.cells = None


def session():
    s = requests.Session()
    retry = Retry(total=5, connect=5, read=5, status=5, backoff_factor=1.5,
                  status_forcelist=(429, 500, 502, 503, 504), allowed_methods=("GET", "HEAD"))
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.mount("http://", HTTPAdapter(max_retries=retry))
    return s


def list_sources(http, base_url):
    LOG.info("Listing GTSPP archives: %s", base_url)
    r = http.get(base_url, timeout=(30, 120))
    r.raise_for_status()
    parser = ListingParser()
    parser.feed(r.text)
    if not parser.entries:
        raise ValueError("No archives found; source listing may have changed")
    groups = {}
    for name, modified in parser.entries.items():
        match = re.fullmatch(r"gtspp4_(at|in|pa)(\d{6})\.tgz", name)
        month = match[2]
        month_bounds(month)  # Validate the date rather than accepting any digits.
        groups.setdefault(month, {})[match[1]] = {"name": name, "modified": modified, "url": urljoin(base_url, name)}
    latest_allowed = datetime.now(UTC).strftime("%Y%m")
    complete = {m: g for m, g in groups.items() if m < latest_allowed and set(g) == set(BASINS)}
    if not complete:
        raise ValueError("No complete previous-month archive groups")
    incomplete = [m for m, g in groups.items() if m < latest_allowed and set(g) != set(BASINS)]
    if incomplete:
        LOG.warning("Incomplete months excluded (missing basin archives): %s", ", ".join(incomplete[-12:]))
    LOG.info("Found %d complete months; latest=%s", len(complete), max(complete))
    return complete


def fingerprint(group):
    data = [(basin, group[basin]["name"], group[basin]["modified"]) for basin in BASINS]
    return hashlib.sha256(json.dumps(data).encode()).hexdigest()


def select_months(groups, state, lookback=4, max_months=8, first=None, last=None, force=False):
    all_months = sorted(groups)
    if first or last:
        if not (first and last) or first > last:
            raise ValueError("Provide --start-month and --end-month together in YYYYMM order")
        start = datetime.strptime(first, "%Y%m")
        finish = datetime.strptime(last, "%Y%m")
        requested = []
        while start <= finish:
            requested.append(start.strftime("%Y%m"))
            start = datetime(start.year + (start.month == 12), start.month % 12 + 1, 1)
        missing = [m for m in requested if m not in groups]
        if missing:
            raise ValueError(f"Missing complete upstream archive groups: {missing}")
        return [m for m in requested if force or state.get("processed", {}).get(m) != fingerprint(groups[m])][:max_months]
    baseline = state.get("baseline")
    if baseline is None:
        # Existing historical production files are already seeded. Establish an
        # observed baseline, not a claim that historical outputs were validated.
        return all_months[-lookback:][:max_months]
    candidates = [m for m in all_months if baseline.get(m) != fingerprint(groups[m])]
    return candidates[:max_months]


def download(http, item, path):
    LOG.info("Downloading %s", item["name"])
    part = path.with_suffix(path.suffix + ".part")
    for attempt in range(3):
        try:
            digest = hashlib.sha256()
            total = 0
            with http.get(item["url"], stream=True, timeout=(30, 120)) as r:
                r.raise_for_status()
                modified = r.headers.get("Last-Modified")
                if modified:
                    from email.utils import parsedate_to_datetime
                    actual = parsedate_to_datetime(modified).astimezone(UTC).strftime("%Y-%m-%d %H:%M")
                    if actual != item["modified"]:
                        raise ValueError(f"Archive changed since listing: {item['name']}; retry on next run")
                with part.open("wb") as f:
                    for chunk in r.iter_content(1024 * 1024):
                        if chunk:
                            f.write(chunk)
                            digest.update(chunk)
                            total += len(chunk)
                expected = r.headers.get("Content-Length")
                if not total or (expected is not None and total != int(expected)):
                    raise ValueError(f"Incomplete download of {item['name']}")
            part.replace(path)
            LOG.info("Downloaded %s (%d bytes)", item["name"], total)
            return {**item, "sha256": digest.hexdigest(), "bytes": total}
        except (requests.RequestException, OSError):
            part.unlink(missing_ok=True)
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)


class LocalDestination:
    def __init__(self, directory):
        self.root = Path(directory)
        self.root.mkdir(parents=True, exist_ok=True)
        self.generation = None

    def read_state(self):
        p = self.root / "state.json"
        return json.loads(p.read_text()) if p.exists() else {}

    def save_state(self, state):
        p = self.root / "state.json.part"
        p.write_text(json.dumps(state, indent=2) + "\n")
        p.replace(self.root / "state.json")

    def publish(self, path, metadata):
        import shutil
        shutil.copyfile(path, self.root / path.name)
        (self.root / (path.name + ".metadata.json")).write_text(json.dumps(metadata, indent=2) + "\n")

    def exists(self, name):
        return (self.root / name).is_file()

    def acquire(self):
        self.lock = (self.root / ".lock").open("x")

    def release(self):
        self.lock.close()
        (self.root / ".lock").unlink()


class CloudDestination:
    def __init__(self, bucket, prefix, state_prefix):
        from google.cloud import storage
        self.bucket = storage.Client().bucket(bucket)
        self.prefix = prefix.strip("/")
        self.state_prefix = state_prefix.strip("/")
        if any(part.startswith("_") for path in (self.prefix, self.state_prefix)
               for part in path.split("/")):
            raise ValueError("Cloud output and state paths must not use underscore prefixes")
        self.generation = 0
        self.lock_generation = None

    def read_state(self):
        blob = self.bucket.get_blob(self.state_prefix + "/state.json")
        self.generation = int(blob.generation) if blob else 0
        return json.loads(blob.download_as_text(if_generation_match=self.generation)) if blob else {}

    def save_state(self, state):
        blob = self.bucket.blob(self.state_prefix + "/state.json")
        blob.upload_from_string(json.dumps(state, indent=2), content_type="application/json", if_generation_match=self.generation)
        self.generation = int(blob.generation)

    def publish(self, path, metadata):
        old = self.bucket.get_blob(self.prefix + "/" + path.name)
        blob = self.bucket.blob(self.prefix + "/" + path.name)
        blob.metadata = {"processor": "gtspp-python-v1", "source_fingerprint": metadata["source_fingerprint"], "month": metadata["month"]}
        blob.upload_from_filename(str(path), content_type="application/x-netcdf", if_generation_match=int(old.generation) if old else 0, checksum="crc32c")
        self.bucket.blob(self.state_prefix + "/runs/" + metadata["run_id"] + "/" + path.name + ".json").upload_from_string(json.dumps(metadata), content_type="application/json", if_generation_match=0)

    def exists(self, name):
        return self.bucket.blob(self.prefix + "/" + name).exists()

    def acquire(self):
        from google.api_core.exceptions import PreconditionFailed
        name = self.state_prefix + "/lock.json"
        previous = self.bucket.get_blob(name)
        generation = 0
        if previous:
            info = json.loads(previous.download_as_text(if_generation_match=int(previous.generation)))
            if datetime.fromisoformat(info["expires"]) > datetime.now(UTC):
                raise RuntimeError("Another GTSPP execution holds the lock")
            generation = int(previous.generation)
        blob = self.bucket.blob(name)
        blob.upload_from_string(json.dumps({"owner": str(uuid.uuid4()), "expires": (datetime.now(UTC) + timedelta(hours=6)).isoformat()}), content_type="application/json", if_generation_match=generation)
        self.lock_generation = int(blob.generation)

    def release(self):
        if self.lock_generation is not None:
            self.bucket.blob(self.state_prefix + "/lock.json").delete(if_generation_match=self.lock_generation)


def get_flag_key():
    from google.cloud import secretmanager
    key = os.getenv("ERDDAP_FLAG_KEY", "").strip()
    if not key:
        resource = os.environ["ERDDAP_FLAG_SECRET"]
        regional = re.fullmatch(r"projects/[^/]+/locations/([^/]+)/secrets/[^/]+/versions/[^/]+", resource)
        options = {"api_endpoint": f"secretmanager.{regional[1]}.rep.googleapis.com"} if regional else None
        key = secretmanager.SecretManagerServiceClient(client_options=options).access_secret_version(request={"name": resource}).payload.data.decode().strip()
    if not re.fullmatch(r"[a-fA-F0-9]{64}", key):
        raise ValueError("ERDDAP flag secret is not a 64-character hex key")
    return key


def flag_erddap(http, key):
    url = os.getenv("ERDDAP_BASE_URL", "https://155.206.244.135/erddap").rstrip("/") + "/setDatasetFlag.txt"
    # No URL or request exception is logged: query parameters contain a secret.
    try:
        response = http.get(url, params={"datasetID": "erdGtsppBest", "flagKey": key}, verify=os.getenv("ERDDAP_INSECURE", "false").lower() != "true", timeout=(30, 120))
    except requests.RequestException:
        raise RuntimeError("ERDDAP flag request failed; reload remains pending") from None
    if response.status_code != 200 or "SUCCESS" not in response.text or "ERROR" in response.text:
        raise RuntimeError(f"ERDDAP flag rejected (HTTP {response.status_code}); reload remains pending")
    LOG.info("ERDDAP flag accepted: erdGtsppBest")


def run(args):
    mode = os.getenv("JOB_MODE", "staging")
    if mode not in ("staging", "production"):
        raise ValueError("JOB_MODE must be staging or production")
    if args.local_output:
        if mode == "production":
            raise ValueError("Local output cannot run in production mode")
        dest = LocalDestination(args.local_output)
    else:
        prefix = os.getenv("PREFIX", "staging/gtspp/points/gtsppNcCF")
        if mode == "production" and prefix.strip("/") != "points/gtsppNcCF":
            raise ValueError("Production output must be points/gtsppNcCF")
        if mode == "staging" and not prefix.startswith("staging/"):
            raise ValueError("Staging output must be under staging/")
        dest = CloudDestination(os.getenv("BUCKET", "erddap-swfsc-erd"), prefix,
            os.getenv("STATE_PREFIX", f"operations/workflow-state/gtspp/{mode}"))
    http = session()
    flag_key = get_flag_key() if mode == "production" and not args.plan_only else None
    dest.acquire()
    try:
        state = dest.read_state()
        groups = list_sources(http, args.source_url)
        selection = select_months(groups, state, args.lookback_months, args.max_months,
                                  args.start_month, args.end_month, args.force)
        # Rebuild a tracked month if its output was removed after a successful run.
        for month in sorted(state.get("processed", {})) if not args.start_month else []:
            if month in groups and month not in selection and any(not dest.exists(month + h + ".nc") for h in "ab"):
                selection.append(month)
        selection = sorted(selection)[:args.max_months]
        LOG.info("Selected months: %s", selection or "none")
        if args.plan_only:
            return
        if "baseline" not in state:
            state["baseline"] = {m: fingerprint(g) for m, g in groups.items()}
            # Do not silently baseline months before actually processing the
            # bootstrap window: pending entries survive failed/limited runs.
            state["bootstrap_pending"] = sorted(groups)[-args.lookback_months:]
        pending = state.get("bootstrap_pending", [])
        if not (args.start_month or args.end_month):
            selection = sorted(set(selection + pending))[:args.max_months]
        # Failed paired publication is retried even for an explicit one-month
        # run whose baseline already existed before the upload began.
        selection = sorted(set(selection + list(state.get("pending_months", {}))))[:args.max_months]
        run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
        uploaded = 0
        for month in selection:
            stats = Counter()
            sources = []
            with tempfile.TemporaryDirectory(prefix="gtspp-", dir=os.getenv("TMPDIR", "/tmp")) as temp:
                temp = Path(temp)
                store = ProfileStore(temp / "profiles.sqlite")
                try:
                    for basin in BASINS:
                        item = groups[month][basin]
                        archive = temp / item["name"]
                        sources.append(download(http, item, archive))
                        store.ingest(archive, month, stats)
                        archive.unlink()
                    store.prepare()
                    names = [s["name"] for s in sources]
                    reports = {}
                    for half in "ab":
                        path = temp / (month + half + ".nc")
                        reports[half] = write_cf(store, month, half, path, names)
                    # Both outputs must validate before either is published.
                    if mode == "production":
                        state["pending_reload"] = True
                    state["pending_subset"] = True
                    state.setdefault("pending_months", {})[month] = fingerprint(groups[month])
                    dest.save_state(state)
                    for half in "ab":
                        dest.publish(temp / (month + half + ".nc"), {
                            "month": month, "run_id": run_id, "source_fingerprint": fingerprint(groups[month]),
                            "sources": sources, "qc": dict(stats), "validation": reports[half]})
                        uploaded += 1
                    state.setdefault("processed", {})[month] = fingerprint(groups[month])
                    state["baseline"][month] = fingerprint(groups[month])
                    state["last_export_at"] = datetime.now(UTC).isoformat(timespec="seconds")
                    state["pending_months"].pop(month, None)
                    state["bootstrap_pending"] = [m for m in state.get("bootstrap_pending", []) if m != month]
                    dest.save_state(state)
                    LOG.info("MONTH_COMPLETE %s %s QC=%s", month, reports, dict(stats))
                finally:
                    store.close()
        # A failed publication must not be mistaken for an unchanged month.
        if state.get("pending_months"):
            raise RuntimeError("Unfinished months remain; retry before flagging")
        if state.get("pending_subset") and isinstance(dest, CloudDestination):
            from gtspp_subset import refresh_cloud_subset
            LOG.info("SUBSET_REFRESH %s", refresh_cloud_subset(dest))
            state["pending_subset"] = False
            dest.save_state(state)
        if mode == "production" and state.get("pending_reload"):
            flag_erddap(http, flag_key)
            state["pending_reload"] = False
            dest.save_state(state)
        LOG.info("SUMMARY uploaded=%d processed_months=%d mode=%s", uploaded, len(selection), mode)
    finally:
        dest.release()
        http.close()


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source-url", default=os.getenv("SOURCE_URL", SOURCE_URL))
    p.add_argument("--start-month", default=os.getenv("START_MONTH") or None)
    p.add_argument("--end-month", default=os.getenv("END_MONTH") or None)
    p.add_argument("--lookback-months", type=int, default=int(os.getenv("LOOKBACK_MONTHS", "4")))
    p.add_argument("--max-months", type=int, default=int(os.getenv("MAX_MONTHS", "8")))
    p.add_argument("--force", action="store_true", default=os.getenv("FORCE", "false").lower() == "true")
    p.add_argument("--plan-only", action="store_true")
    p.add_argument("--local-output", help="Local staging only; no GCP credentials or ERDDAP flagging")
    return p


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = parser().parse_args()
    if args.lookback_months < 1 or args.max_months < 1:
        raise SystemExit("Month limits must be positive")
    try:
        run(args)
    except Exception as exc:
        LOG.error("WORKFLOW_FAILED %s", exc)
        raise SystemExit(1)
