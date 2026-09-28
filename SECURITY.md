# Security policy

Santa Server decides which programs may run on your Macs, so security reports are very welcome.

## Reporting a vulnerability

Please **don't open a public issue**. Report it privately through GitHub:
*Security* → *Report a vulnerability* on this repository. Include the affected version, what an attacker can do,
and the steps to reproduce.

You get an answer within a week. Once a fix is released, the advisory is published with credit to you,
unless you prefer otherwise.

## Supported versions

Only the latest release (and `main`) gets security fixes.

## Scope

In scope: the sync endpoints, the console, the request form, the sign-in, the handling of downloaded releases and
uploaded binaries, and the container image. Out of scope: Santa itself (report those to
[North Pole Security](https://github.com/northpolesec/santa/security)) and issues that need a compromised
administrator account.
