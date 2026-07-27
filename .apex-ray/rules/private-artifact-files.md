---
id: private-artifact-files
title: Never commit private Apex Ray artifacts or credential files
severity: critical
mode: strict
paths:
  - ".apex-ray/config.local.yml"
  - ".apex-ray/cache/**"
  - ".apex-ray/telemetry/**"
  - ".apex-ray/reports/**"
  - ".apex-ray/eval/telemetry/**"
  - ".apex-ray/eval/runs/**"
  - ".apex-ray/evals/runs/**"
  - ".[eE][nN][vV]"
  - ".[eE][nN][vV].*"
  - "*.[pP][eE][mM]"
  - "*.[kK][eE][yY]"
  - "**/.[eE][nN][vV]"
  - "**/.[eE][nN][vV].*"
  - "**/*.[pP][eE][mM]"
  - "**/*.[kK][eE][yY]"
exclude_paths:
  - ".[eE][nN][vV]*.[eE][xX][aA][mM][pP][lL][eE]"
  - ".[eE][nN][vV]*.[sS][aA][mM][pP][lL][eE]"
  - ".[eE][nN][vV]*.[tT][eE][mM][pP][lL][aA][tT][eE]"
  - "**/.[eE][nN][vV]*.[eE][xX][aA][mM][pP][lL][eE]"
  - "**/.[eE][nN][vV]*.[sS][aA][mM][pP][lL][eE]"
  - "**/.[eE][nN][vV]*.[tT][eE][mM][pP][lL][aA][tT][eE]"
---
These paths are machine-local, generated, or credential-bearing and must not
enter a commit.

Remove the file from the change, rotate any exposed credential, and replace
private inputs with a minimal anonymized fixture when regression coverage is
needed.
