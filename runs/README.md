# Pipeline Integration Run Requests

Version-controlled, one-combination run requests live here. Use
`sdi-integration identify-run` to validate one committed request and its
referenced inputs before assigning an Execution ID.

The six requests under `s-04/` are ordered C-01 through C-06. Their `-fixture`
file names predate the implemented Stages. On Jenkins, each run composes with
the configured model, builds the placed images, runs CV, and deploys the
composition to the stack's K3s stand-in cluster, so it provides Validation and
deployment evidence. Under `dispatch-local`, image build and CD remain Fixtures
and CV does not run. Neither route evaluates KPIs.
