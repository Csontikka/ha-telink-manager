# Security Policy

## Supported Versions

| Version | Supported |
|---------|-----------|
| Latest release | Yes |
| Older versions | No |

Only the latest release receives security updates. Please upgrade before reporting.

## Reporting a Vulnerability

**Do not open a public issue for security vulnerabilities.**

Instead, use [GitHub's private vulnerability reporting](https://github.com/Csontikka/ha-telink-manager/security/advisories/new) to report security issues confidentially.

Please include:
- Description of the vulnerability
- Steps to reproduce
- Potential impact
- Suggested fix (if any)

You will receive an initial response within 72 hours. Once confirmed, a fix will be released as soon as possible with credit to the reporter (unless anonymity is requested).

## Scope

This policy covers the `ha-telink-manager` Home Assistant custom integration. Issues in the PVVX/ATC firmware itself should be reported to the [pvvx/ATC_MiThermometer](https://github.com/pvvx/ATC_MiThermometer) project.

Note: this integration can write privileged settings to BLE thermometers (MAC address, encryption bind key, factory reset). These operations are clearly marked as dangerous in the UI and require admin access to the Home Assistant panel.
