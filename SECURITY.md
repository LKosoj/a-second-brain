# Security policy

## Supported versions

Security fixes are applied to the latest release.

## Reporting

Do not open a public issue containing credentials, private vault content, or
personal data. Use the repository host's private security-reporting channel
once the public remote is configured.

## Operator responsibilities

- Keep `.env`, `vault/`, `.vault-backups/`, logs, and runtime state outside
  public version control.
- Restrict filesystem access to the service account.
- Keep the private key for encrypted vault backups off the application host.
- Pin and review dependencies and AI CLI updates.
- Run `./scripts/doctor.sh` before enabling services.
- Rotate any credential exposed in a commit, log, issue, or chat.

This project processes private material through services configured by the
operator. Review the privacy and retention terms of every selected AI,
transcription, search, and task provider.
