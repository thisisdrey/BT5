# Q2247: Panic on malformed request in stringWriter.WriteBytes

## Question
Can an unauthenticated command line panic `stringWriter.WriteBytes` (sshd/writer.go) while parsing an unauthenticated request?

## Target
- File/function: `sshd/writer.go` -> `stringWriter.WriteBytes` (declared at sshd/writer.go:25)
- Entrypoint: Unauthenticated TCP/UDP connection to a locally reachable Nebula listener (SSH admin or DNS)
- Attacker controls: an unauthenticated command line; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Fuzz the request parser with malformed and truncated input.
- Invariant to test: Request parsing never panics regardless of input.
- Expected Immunefi impact: Remote crash of the node via its listener.
- Fast validation: Go fuzz target over `stringWriter.WriteBytes` asserting zero panics.
