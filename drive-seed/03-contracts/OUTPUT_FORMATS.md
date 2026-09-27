# Output registry v1

Universal minimum: exit state, times, raw stdout/stderr plus decoded text, frozen source/environment
identity, run manifest. An absent collector is not a successful collector.

| kind | format / media | required semantics |
|---|---|---|
| console | raw bytes + text/plain UTF-8 view | stdout/stderr separately; byte hash; cap/decoding flags |
| application_log | application-log/v1 / application/x-ndjson | timestamp, level, service, event, message; schema errors retained |
| test_result | junit-xml / application/xml | pass/fail/skip/error counts, cases, failure details, parser status |
| coverage | cobertura-xml / application/xml | actual measured line/branch totals, no inferred percentages |
| report | text/markdown | concise result with provenance and limits |
| screenshot | image/png or image/jpeg | dimensions, media type, originating run |
| artifact | registered media type | exact bytes, size, digest, role; never auto-execute |
| artifact | bundle-index/v1 / application/json | explicit contained output IDs; not arbitrary directory traversal |

Raw originals may stay local; cloud output is privacy-filtered with its own hash and lineage.
Truncation, redaction and parser failure MUST be visible. HTML/SVG remain untrusted active content.
Reference schema: run-manifest.schema.json. Application schema evolution must be versioned.

Operations with no program execution use operation-manifest.schema.json and run_ids=[]; provisioning logs are still valid typed outputs. Shared per-output fields are in output.schema.json.
