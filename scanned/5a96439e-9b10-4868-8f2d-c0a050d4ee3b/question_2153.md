# Q2153: Panic on malformed request in stringWriter.Write

## Question
Can a flood of half-open connections panic `stringWriter.Write` (sshd/writer.go) while parsing an unauthenticated request?

## Target
- File/function: `sshd/writer.go` -> `stringWriter.Write` (declared at sshd/writer.go:20)
- Entrypoint: Unauthenticated TCP/UDP connection to a locally reachable Nebula listener (SSH admin or DNS)
- Attacker controls: a flood of half-open connections; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Fuzz the request parser with malformed and truncated input.
- Invariant to test: Request parsing never panics regardless of input.
- Expected Immunefi impact: Remote crash of the node via its listener.
- Fast validation: Go fuzz target over `stringWriter.Write` asserting zero panics.
