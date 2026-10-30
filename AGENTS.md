# Credential handling

- Never print, dump, or broadly search `.env`, credential files, OAuth tokens, or API-key files.
- Exclude `.env` and credential paths from `rg`, `grep`, `find -exec`, shell globs, and diagnostic commands. Do not pass those files as explicit search targets.
- To check whether configuration is present, use a file-presence check or inspect only an allowlisted variable name while emitting only `set`/`unset`; never emit its value.
- If a non-secret value must be checked, read only that specific allowlisted key and redact any value that could contain a credential before displaying or logging it.
- Before running commands that may print environment/configuration data, review the command for secret exposure. Never include secrets in tool arguments, logs, error summaries, or user-facing responses.
- If a credential is accidentally exposed, stop the affected job when safe, notify the user without repeating the credential, and recommend immediate rotation.
