# Q1410: Unbounded read/allocation in matchCommand

## Question
Can a partially written request left open make `matchCommand` (sshd/command.go) buffer or allocate without a size limit before the request is even parsed?

## Target
- File/function: `sshd/command.go` -> `matchCommand` (declared at sshd/command.go:86)
- Entrypoint: Unauthenticated TCP/UDP connection to a locally reachable Nebula listener (SSH admin or DNS)
- Attacker controls: a partially written request left open; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send a request line that never terminates and measure retained memory.
- Invariant to test: Reads from an unauthenticated client are hard-bounded in size and time.
- Expected Immunefi impact: Memory exhaustion of the node from an unauthenticated local connection.
- Fast validation: Unit test streaming unbounded input to `matchCommand` and asserting a bounded cap and disconnect.
