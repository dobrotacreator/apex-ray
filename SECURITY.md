# Security Policy

## Supported Versions

Apex Ray is currently pre-1.0. Security fixes target the latest `main` branch until versioned releases are established.

## Reporting A Vulnerability

Please do not open public issues for suspected vulnerabilities. Report them privately through GitHub security advisories if enabled, or contact the maintainers through the private channel listed in the GitHub repository.

Include:

- affected version or commit SHA;
- reproduction steps;
- impact;
- whether credentials, source code, or private telemetry may be exposed.

## Data And LLM Privacy

Apex Ray sends selected diff and context-pack content to the configured local LLM CLI provider when LLM review is enabled. Review the provider's own privacy and retention behavior before using Apex Ray on private code.

Telemetry and caches are local files. They may contain repository paths, model names, token estimates, coverage metadata, and finding counts. They should be ignored by git unless a team intentionally curates and reviews a specific artifact.
