# Project Documentation

This area contains role-neutral project context and operational guidance:

- `pipeline-integration-trigger.md`: protect, trigger, observe, cancel, and
  retrieve one exact GitHub Pipeline integration run.
- `pipeline-events.md`: the `[sdi]` and `PRISM_EVENT` lines a run prints on
  stderr, their fields and steps, and the `check-events` conformance checker.
- `scaffold-maintainer.md`: contract authorities, ownership seams, versioning,
  adapter replacement, verification, and deferred Domain capabilities.
- `installation-readiness-checklist.md`: reusable sanitized evidence checklist
  for security, reconstruction, restoration, live-run, recovery, and walkthrough
  acceptance.

Jenkins host setup, lifecycle, recovery, rotation, troubleshooting, and safe
shutdown are documented in `deployment/jenkins/README.md`. The GitHub handoff
runners run as containers in the Jenkins stack and are documented in the same
file.
