# Security

## Reporting a problem

Please report security problems privately, through GitHub's private vulnerability reporting: open the
repository's **Security** tab and choose **Report a vulnerability**. Please do not open a public issue, and do not
include real logins, tokens or network details from your own setup.

You will get an answer in that report. A fix is released as a new pinned version, and the report is published once
people can update.

## What counts

Anything that lets this server do more than the person allowed, for example:

- a change sent while the server was started with `--read-only`;
- a request sent to a different path, site or host than the tool call named (a path piece with `/`, `?`, `#` or
  `..` must be refused before anything is sent);
- a write to a site the login may only read, once the server knows the login's scopes;
- a login token, password or pre-shared key showing up in a reply, an error or a log;
- the package downloading or running anything at install or import time.

## Supported versions

Only the latest release gets fixes.
