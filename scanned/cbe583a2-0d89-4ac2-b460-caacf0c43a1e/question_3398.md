# Q3398: Panic on malformed request in matchCommand

## Question
Can an oversized request line panic `matchCommand` (sshd/command.go) while parsing an unauthenticated request?

## Target
- File/function: `sshd/command.go` -> `matchCommand` (declared at sshd/command.go:86)
- Entrypoint: Unauthenticated TCP/UDP connection to a locally reachable Nebula listener (SSH admin or DNS)
- Attacker controls: an oversized request line; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Fuzz the request parser with malformed and truncated input.
- Invariant to test: Request parsing never panics regardless of input.
- Expected Immunefi impact: Remote crash of the node via its listener.
- Fast validation: Go fuzz target over `matchCommand` asserting zero panics.
