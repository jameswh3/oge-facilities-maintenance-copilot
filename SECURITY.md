# Security Policy

## Supported version

Security fixes are applied to the latest version on the `main` branch.

## Reporting a vulnerability

Use GitHub private vulnerability reporting for this repository. Include:

- the affected file or component
- reproduction steps
- expected and observed behavior
- potential impact
- any suggested mitigation

Do not include credentials, access tokens, tenant data, customer data, or live
operational records in a public issue. If private vulnerability reporting is not
available, open a public issue requesting a private maintainer contact without
including vulnerability details.

## Credential exposure

If a credential is accidentally committed:

1. Revoke or rotate it immediately.
2. Remove it from the repository and Git history.
3. Review relevant Azure, Microsoft 365, Fabric, and Power Platform audit logs.
4. Report the exposure privately.

The repository intentionally ignores `.env`, generated deployment state, Copilot
connection metadata, and local caches. Contributors must verify staged content before
every push.
