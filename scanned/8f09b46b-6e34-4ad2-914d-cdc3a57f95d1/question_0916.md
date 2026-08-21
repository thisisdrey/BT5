# Q0916: Unbounded read/allocation in stringWriter.WriteBytes

## Question
Can a DNS query for an arbitrary VPN address make `stringWriter.WriteBytes` (sshd/writer.go) buffer or allocate without a size limit before the request is even parsed?

## Target
- File/function: `sshd/writer.go` -> `stringWriter.WriteBytes` (declared at sshd/writer.go:25)
- Entrypoint: Unauthenticated TCP/UDP connection to a locally reachable Nebula listener (SSH admin or DNS)
- Attacker controls: a DNS query for an arbitrary VPN address; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send a request line that never terminates and measure retained memory.
- Invariant to test: Reads from an unauthenticated client are hard-bounded in size and time.
- Expected Immunefi impact: Memory exhaustion of the node from an unauthenticated local connection.
- Fast validation: Unit test streaming unbounded input to `stringWriter.WriteBytes` and asserting a bounded cap and disconnect.
