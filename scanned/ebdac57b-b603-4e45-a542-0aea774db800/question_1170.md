# Q1170: Unbounded read/allocation in session.handleChannels

## Question
Can a malformed DNS TXT query make `session.handleChannels` (sshd/session.go) buffer or allocate without a size limit before the request is even parsed?

## Target
- File/function: `sshd/session.go` -> `session.handleChannels` (declared at sshd/session.go:44)
- Entrypoint: Unauthenticated TCP/UDP connection to a locally reachable Nebula listener (SSH admin or DNS)
- Attacker controls: a malformed DNS TXT query; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send a request line that never terminates and measure retained memory.
- Invariant to test: Reads from an unauthenticated client are hard-bounded in size and time.
- Expected Immunefi impact: Memory exhaustion of the node from an unauthenticated local connection.
- Fast validation: Unit test streaming unbounded input to `session.handleChannels` and asserting a bounded cap and disconnect.
