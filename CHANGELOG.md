# Changelog

## 0.2.0

Default to production with paused scheduling. Remove development and validation
files from the distributed production package. Add hidden flag-secret setup and
surface existing repository authentication errors directly. August staging and
full-file comparisons completed successfully.

## 0.1.1

Use a reusable real temporary profile file instead of NetCDF's in-memory file
interface, which the first Cloud Run staging execution rejected with EPERM.

## 0.1.0

Initial GTSPP Cloud Run processor, staging deployment, revision tracking, CF validation, subset generation, and VM installation helper.
