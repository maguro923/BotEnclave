# Security Policy

## Reporting a vulnerability

Please do not open a public issue for security vulnerabilities.

Use GitHub's private vulnerability reporting ("Report a vulnerability" under
the Security tab of this repository) so the report stays private until a fix
is available.

Please include, as far as you can:

- the version or commit affected
- a description of the impact
- steps to reproduce, or a proof of concept

## Scope notes

BotEnclave is designed to run untrusted bot code with OS-level isolation
between bots. Reports that break that isolation — one bot reading another
bot's files, environment, or console, or a web user reaching another user's
bots — are especially valuable and in scope.
