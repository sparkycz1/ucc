---
name: Bug report
about: Something is broken or behaves unexpectedly
title: ""
labels: bug
assignees: ""
---

## Describe the bug

A clear and concise description of what the bug is.

## To reproduce

Steps to reproduce the behavior — the more exact, the faster this gets
fixed (which page, which button/field, what you typed, in what order).

1.
2.
3.

## Expected behavior

A clear and concise description of what you expected to happen instead.

## Priority

What's the impact — how critical is this to fix? P0 (breaks the app for
everyone) .. P4 (cosmetic, no rush).
[Reference — Google Issue Tracker priority levels](https://developers.google.com/issue-tracker/concepts/issues#priority)

## Screenshots/video

If applicable, add screenshots or a screen recording — mark the area
that's affected.

## Environment

- **ucc version**: the `vX.Y.Z` shown in the page footer (or `git rev-parse HEAD` if running from a checkout)
- **Deployment**: Docker Compose — bundled Caddy, or your own reverse proxy?
- **Browser**: [e.g. Chrome 128, Firefox, Safari] — desktop or mobile?
- **Auth method involved, if relevant**: local / LDAP / OIDC
- **UniFi side, if this is about a Fabric/console/device**: console model, UniFi OS version, Network application version, device model and firmware

## Relevant logs

`docker compose logs web` (or `worker`/`beat` if more relevant) — paste
just the lines around when it happened, not the whole log. Double-check
there's no password, token or **Site Manager API key** in what you paste.

```
paste here
```

## Additional context

Add any other context about the problem here.
