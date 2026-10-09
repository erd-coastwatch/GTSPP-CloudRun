# GTSPP Cloud Run

Monthly GTSPP processing for erdGtsppBest, based on erd-coastwatch/cloud-run-template.

## How the two scheduled parts work

This workflow requires both the Cloud Scheduler job and a small installer on the
ERDDAP VM. Deploying Cloud Run does **not** install the VM helper or its cron entry.
Both scripts are maintained in this repository.

| Component | Schedule / trigger | Responsibility |
| --- | --- | --- |
| Cloud Scheduler → gtspp-monthly-processor | 10:00 Pacific on the 10th | Fetch NCEI archives, apply QC, publish CF files, generate the full subset JSON, and send the dataset flag |
| Root cron → gtspp-install-subset on erd-erddap | Every 15 minutes | Install completed subset JSON locally, update the GTSPP ingestion-history date, and request a hard reload when either changes |

ERDDAP reads observations through the existing rclone mount, but its pre-generated
subset JSON is a local content file. That JSON supplies the distinct trajectory,
organization, type, platform and cruise choices in the subset interface. Cloud Run
writes the JSON to GCS; the VM helper copies it to ERDDAP's content directory.
The helper implements the subset/history/reload steps from Roy's original workflow;
the installer and cron arrangement were written for this cloud migration.

The helper does not download or process source profiles. Each poll downloads the
generated JSON and state, refuses unfinished processing, and compares them with the
installed copy. An unchanged result causes no XML write or additional hard reload.
Cloud Run's dataset flag may arrive before this local installation; the helper's
subsequent hard reload makes ERDDAP use the installed subset and updated history.
A successful Cloud Run execution alone does not verify completion of the VM step.

## Production rollout

On your Mac, from this folder:

1. Get the erdGtsppBest dataset-specific flag key from the cloud ERDDAP Daily Report.
   Copy only the value after flagKey=, not flagKeyKey. Do not commit it or paste it into chat.
2. Store the key (hidden prompt), deploy, then execute:

```bash
python3 scripts/setup_flag_secret.py
bash deploy_job.sh
gcloud run jobs execute gtspp-monthly-processor --project=ggn-nmfs-swfscerddap-dev-1 --region=us-east4 --wait
```

Deployment defaults to production v0.2.0 and creates a paused monthly scheduler.
The first run rebuilds the latest four complete months (June–September 2026 at rollout).
Older seeded files are preserved; their observed archive revisions form a baseline,
without claiming they have been revalidated.

3. Copy the subset installer from your Mac:

```bash
gcloud compute scp scripts/install_subset_on_vm.sh erd-erddap:~/gtspp-install-subset --project=ggn-nmfs-swfscerddap-dev-1 --zone=us-east4-a
```

After production succeeds, on **erd-erddap**:

```bash
sudo install -m 0755 ~/gtspp-install-subset /usr/local/sbin/gtspp-install-subset
sudo /usr/local/sbin/gtspp-install-subset
sudo crontab -e
```

Add this line, preserving existing cron entries:

```cron
*/15 * * * * /usr/local/sbin/gtspp-install-subset >> /var/log/gtspp-subset.log 2>&1
```

Root's gcloud needs bucket read access. The installer atomically installs subset JSON,
updates only the GTSPP ingestion-history date in datasets.xml (with backup), and
requests a hard reload when something changes. It rejects incomplete processing state.
The rclone directory cache is five minutes. Keep GTSPP excluded from Roy-to-production sync.

4. After confirming ERDDAP loads the new files and the installer succeeds, on your Mac:

```bash
gcloud scheduler jobs resume gtspp-monthly-schedule --project=ggn-nmfs-swfscerddap-dev-1 --location=us-east4
```

The monthly schedule is 10:00 America/Los_Angeles on the 10th.

## Maintaining the deployed workflow

Normal monthly updates need no source-code edits or manual date changes. Keep both
schedules enabled. The monthly processor detects revisions; the VM polls for the
completed subset and updates the local ingestion date automatically.

| Change | What to update |
| --- | --- |
| Processor, Python dependencies, or output metadata | Edit scripts/config, build and deploy a new image with a new TAG; the VM helper need not be reinstalled unless it also changed |
| VM installer script | Copy scripts/install_subset_on_vm.sh to erd-erddap and repeat sudo install; a Cloud Run redeploy does not update the installed VM copy |
| Bucket, state prefix, or production output path | Update both the cloud configuration and VM helper as applicable, plus ERDDAP's fileDir/rclone mapping if the visible path changes |
| ERDDAP VM, local content directory, or hard-flag directory | Reinstall the helper and root cron on the target VM; update CONTENT_DIR/FLAG_DIR defaults or set them in the cron command |
| ERDDAP flag key or URL | Update Secret Manager or deployment configuration; redeploy when changing the configured secret resource or URL |
| Monthly cadence | Change SCHEDULE/TIME_ZONE in deploy_job.sh or deployment overrides; the VM cron is independent |

The setup script adds a new secret version and grants access to the configured
runtime service account. The job reads the latest version on each execution.
The default deployment recreates and pauses the monthly scheduler, even on a
redeploy. Resume it after verifying the update, or explicitly use
ENABLE_SCHEDULE=true when intending an enabled schedule. It does not change the
VM cron. Do not rerun secret setup for ordinary code or documentation updates.

Default VM locations:

| Item | Path |
| --- | --- |
| Installed helper | /usr/local/sbin/gtspp-install-subset |
| Cron definition | Root's crontab (sudo crontab -e) |
| Installer log | /var/log/gtspp-subset.log |
| Local subset JSON | /usr/local/tomcat_erddap/content/erddap/subset/erdGtsppBest.json |
| Dataset definitions | /usr/local/tomcat_erddap/content/erddap/datasets.xml |
| Hard flag | /mnt/disks/erddap-data/erddap/hardFlag/erdGtsppBest |

The installer creates timestamped datasets.xml.gtspp-*.bak files next to the XML
before changing its GTSPP history. The root account needs gcloud, bucket read access,
Python 3 and flock. Check these again if rebuilding the VM or changing its identity.

On **erd-erddap**, verify the installed step and dataset after a monthly run:

```bash
sudo crontab -l
sudo tail -30 /var/log/gtspp-subset.log
sudo /usr/local/sbin/gtspp-install-subset
curl -fsS 'http://127.0.0.1:8080/erddap/info/erdGtsppBest/index.csv' | grep -E 'time_coverage_(start|end)'
```

Expected installer output is either "updated and reload requested" or "already
current". Allow time for ERDDAP to reload before checking coverage. If Cloud Run
succeeds but VM installation fails, fix the VM access/path problem and rerun the
helper; do not reprocess the source months just to install JSON. If hard-flag
creation failed after the local files changed, fix the flag directory and explicitly
touch its erdGtsppBest flag to request the missed reload.

To stop automatic updates, pause gtspp-monthly-schedule and remove only this
workflow's root cron line. Leave other VM cron entries and production data intact.

## Configuration and recovery

- Project/region: ggn-nmfs-swfscerddap-dev-1 / us-east4.
- Job: gtspp-monthly-processor; 4 CPU, 8 GiB, four-hour timeout, one task, no task retries.
- Service account: swfscerddap-run-sa1@ggn-nmfs-swfscerddap-dev-1.iam.gserviceaccount.com.
- Output: gs://erddap-swfsc-erd/points/gtsppNcCF/.
- State/reports/subset: gs://erddap-swfsc-erd/_processing/gtspp/production/.
- Regional secret: erddap-flag-erd-gtspp-best, us-east4.
- ERDDAP: https://155.206.244.135/erddap, temporary insecure TLS setting.

Environment overrides: PROJECT_ID, REGION, BUCKET, SERVICE_ACCOUNT, TAG, CPU,
MEMORY, FLAG_SECRET, ERDDAP_BASE_URL, ERDDAP_INSECURE. FLAG_SECRET_SCOPE=global
supports an existing global secret; the setup script creates regional secrets.
JOB_MODE=staging is available for later tests; production clears fixed month limits.
The Artifact Registry repository from staging must exist. Authentication/IAM errors
stop deployment instead of triggering a mistaken repository creation.

Source: https://www.ncei.noaa.gov/data/oceans/gtspp/bestcopy/netcdf/
Only complete months with all three basin archives are processed. Later runs detect
new and revised months across the full source listing, up to eight months per run.
Output YYYYMMa.nc covers days 1–15; YYYYMMb.nc covers the remainder, using classic
NetCDF3 CF TrajectoryProfile with the same 14 variables as Roy's reference.

QC/grouping follow Roy's supplied Java. Temporary on-disk profiles work in Cloud Run.
Both halves are validated before publication. Durable pending work, archive hashes,
manifests, QC counts and a six-hour lock live outside the dataset directory.
Historical files are not deleted. Re-execute failed runs without deleting state.
A crashed run's lock expires after six hours; never remove an active execution's lock.

```bash
gcloud run jobs executions list --job=gtspp-monthly-processor --project=ggn-nmfs-swfscerddap-dev-1 --region=us-east4 --limit=5
```

Successful logs contain MONTH_COMPLETE, SUBSET_REFRESH, SUMMARY and exit(0).
Explicit backfill:

```bash
gcloud run jobs execute gtspp-monthly-processor --project=ggn-nmfs-swfscerddap-dev-1 --region=us-east4 --args=--start-month,202605,--end-month,202608,--force --wait
```

## August verification

Staging succeeded October 9, 2026. All 128,336 shared first-half profiles and
107,888 shared second-half profiles matched Roy's measurements at tolerance 1e-5,
with no differing shared profiles. The 17 reference-only stations were absent from
all three current NCEI archives. Cloud added 157 first-half and 30,876 second-half profiles.

The production package excludes tests, comparison scripts, validation downloads,
development requirements, unused template stubs and local environments.
Runtime QC, output validation and recovery remain.
