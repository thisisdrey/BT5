# Q3235: Panic on malformed request in session.handleChannels

## Question
Can a flood of half-open connections panic `session.handleChannels` (sshd/session.go) while parsing an unauthenticated request?

## Target
- File/function: `sshd/session.go` -> `session.handleChannels` (declared at sshd/session.go:44)
- Entrypoint: Unauthenticated TCP/UDP connection to a locally reachable Nebula listener (SSH admin or DNS)
- Attacker controls: a flood of half-open connections; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Fuzz the request parser with malformed and truncated input.
- Invariant to test: Request parsing never panics regardless of input.
- Expected Immunefi impact: Remote crash of the node via its listener.
- Fast validation: Go fuzz target over `session.handleChannels` asserting zero panics.
