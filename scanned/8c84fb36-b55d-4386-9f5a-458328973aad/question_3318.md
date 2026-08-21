# Q3318: Panic on malformed request in session.handleRequests

## Question
Can an unauthenticated command line panic `session.handleRequests` (sshd/session.go) while parsing an unauthenticated request?

## Target
- File/function: `sshd/session.go` -> `session.handleRequests` (declared at sshd/session.go:63)
- Entrypoint: Unauthenticated TCP/UDP connection to a locally reachable Nebula listener (SSH admin or DNS)
- Attacker controls: an unauthenticated command line; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Fuzz the request parser with malformed and truncated input.
- Invariant to test: Request parsing never panics regardless of input.
- Expected Immunefi impact: Remote crash of the node via its listener.
- Fast validation: Go fuzz target over `session.handleRequests` asserting zero panics.
