# Pipeline Integration Run Requests

Version-controlled, one-combination run requests live here. Use
`sdi-integration identify-run` to validate one committed request and its
referenced inputs before assigning an Execution ID.

The six requests under `s-04/` are ordered C-01 through C-06. Their `-fixture`
file names predate the implemented composition Stage: each run now composes with
the configured model, while image build, CV, and CD remain Fixtures. They provide
no Validation, deployment, or KPI evidence.
