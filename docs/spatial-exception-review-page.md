# Spatial exception review page

The Solara page is available at the automatically discovered route
\`/spatial_exceptions\` when the application is run from
\`tomorrowcities.pages\`.

## Configuration
Set \`TCDSE_ADLS_ACCOUNT_URL\` and \`TCDSE_ADLS_FILESYSTEM\` in the application
environment. The page uses \`DefaultAzureCredential\` and requires the app's
managed identity to have blob read/list/create permissions on the configured
filesystem. It does not modify source Silver objects.

## Workflow
1. Refresh the review queue to list pending and decided exceptions.
2. Register an exception using the source object path, SHA-256 of the complete
   shapefile component set, stable feature key, source row index, issue and
   allowlisted geometry evidence.
3. Select a pending record, enter reviewer and rationale, then choose Retain,
   Exclude or Defer.
4. Retain maps to inclusion in a future Gold candidate; Exclude maps to
   omission; Defer remains unresolved and blocks the spatial exception gate.

The reviewer field is an audit label, not authentication. Keep the page behind
the application's authenticated access controls. Do not put personal or
sensitive source attributes into review evidence.

## Current Kalobeyei case
The observed row index 325 is not a durable feature key. Before creating its
review record, calculate a reproducible SHA-256 manifest over the complete
source Shapefile component set and bind the feature key to that exact source
version. The UI pre-fills the observed bounds and area for convenience, but
the reviewer must verify these values against the current source and approved
settlement boundary.

## Gold integration limitation
This page persists decisions and displays the exception gate. The Gold
transformation must separately call \`evaluate_spatial_exception_gate\` and
\`apply_spatial_exception_decisions\` before export. It must not treat the
page's status message as a Gold release approval; other Gold contracts and
quality checks remain mandatory.
