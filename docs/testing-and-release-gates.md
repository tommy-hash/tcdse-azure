# TCDSE testing and release gates

## Purpose

TCDSE changes should be validated on a pull request before they are merged into
`main`. The automated test workflow is a safety net for application logic,
geospatial processing, boundary review and Gold-output contracts. It does not
replace human review, a production smoke test, or data-governance approval.

## Automated pull-request checks

The GitHub Actions workflow at
`.github/workflows/tcdse-tests.yml` runs when code is pushed to `main`, when
a pull request targets `main`, or when manually dispatched.

The job:

1. Uses Python 3.10, matching the current application Dockerfile.
2. Installs the GDAL, GEOS and PROJ system libraries used by the geospatial stack.
3. Installs the TCDSE project, pytest and the Azure Blob SDK used by the storage
   adapters.
4. Compiles the application and test modules to catch Python syntax errors.
5. Runs the complete test suite under `tests/`.

The workflow uses read-only repository permissions and does not receive Azure
credentials, publish container images, modify ADLS, or deploy the application.

The existing pull-request Docker build remains a separate check. The existing
main-branch build-and-deploy workflow remains separate from this test workflow.

## Run tests locally (Windows PowerShell)

From the repository root:

```powershell
py -3.10 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e .
python -m pip install pytest azure-storage-blob
python -m compileall -q tomorrowcities tests
python -m pytest tests -v --tb=short -ra
```

On Linux, create and activate the virtual environment with
`python3.10 -m venv .venv` and `source .venv/bin/activate`. Install the
system geospatial libraries (GDAL, GEOS and PROJ) before installing the Python
dependencies.

## Recommended merge procedure

1. Create a feature branch from the latest `main`.
2. Make the change and add or update tests.
3. Open a pull request targeting `main`.
4. Wait for the automated test workflow and Docker build check to complete.
5. Review failures; do not merge while a required check is failing or has not
   completed.
6. Review the code and data implications, then merge.
7. Confirm the main-branch deployment workflow completed successfully.
8. Perform an application smoke test and, where relevant, a controlled
   non-production geospatial/ADLS integration test.

## Make CI a required merge check

After this workflow has run at least once on a pull request, a repository
administrator should configure GitHub branch protection or a repository ruleset
for `main`:

- Require a pull request before merging.
- Require the `Python 3.10 geospatial test suite` status check.
- Also require the existing Docker pull-request build check.
- Require the branch to be up to date before merging if that suits the team's
  integration practice.
- Restrict bypass permissions to trusted administrators.

The exact status-check label shown by GitHub may include the workflow/job
context. Select the check as it appears in the branch-protection UI after its
first run. This repository change does not itself enable branch protection;
that is a repository administration setting.

## What CI does not prove

A passing unit-test suite does not prove that:

- Azure credentials, RBAC, ADLS paths or production network access are correct.
- The production container can reach every required service.
- A candidate boundary is authoritative or suitable for a particular study.
- A human review decision is correct.
- A provisional approximation is eligible for Gold publication.

Boundary approval and Gold release must continue to use the existing
fail-closed validation and human-review controls. CI must never approve a
boundary or publish Gold as a side effect.
