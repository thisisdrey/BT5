# Q2058: Panic on malformed request in stringWriter.WriteLine

## Question
Can a partially written request left open panic `stringWriter.WriteLine` (sshd/writer.go) while parsing an unauthenticated request?

## Target
- File/function: `sshd/writer.go` -> `stringWriter.WriteLine` (declared at sshd/writer.go:16)
- Entrypoint: Unauthenticated TCP/UDP connection to a locally reachable Nebula listener (SSH admin or DNS)
- Attacker controls: a partially written request left open; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Fuzz the request parser with malformed and truncated input.
- Invariant to test: Request parsing never panics regardless of input.
- Expected Immunefi impact: Remote crash of the node via its listener.
- Fast validation: Go fuzz target over `stringWriter.WriteLine` asserting zero panics.
