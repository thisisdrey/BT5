# Q3646: Panic on malformed request in session.Close

## Question
Can a partially written request left open panic `session.Close` (sshd/session.go) while parsing an unauthenticated request?

## Target
- File/function: `sshd/session.go` -> `session.Close` (declared at sshd/session.go:174)
- Entrypoint: Unauthenticated TCP/UDP connection to a locally reachable Nebula listener (SSH admin or DNS)
- Attacker controls: a partially written request left open; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Fuzz the request parser with malformed and truncated input.
- Invariant to test: Request parsing never panics regardless of input.
- Expected Immunefi impact: Remote crash of the node via its listener.
- Fast validation: Go fuzz target over `session.Close` asserting zero panics.
