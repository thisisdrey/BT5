# Q2339: Unbounded read/allocation in SSHServer.Stop

## Question
Can a partially written request left open make `SSHServer.Stop` (sshd/server.go) buffer or allocate without a size limit before the request is even parsed?

## Target
- File/function: `sshd/server.go` -> `SSHServer.Stop` (declared at sshd/server.go:266)
- Entrypoint: Unauthenticated TCP/UDP connection to a locally reachable Nebula listener (SSH admin or DNS)
- Attacker controls: a partially written request left open; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send a request line that never terminates and measure retained memory.
- Invariant to test: Reads from an unauthenticated client are hard-bounded in size and time.
- Expected Immunefi impact: Memory exhaustion of the node from an unauthenticated local connection.
- Fast validation: Unit test streaming unbounded input to `SSHServer.Stop` and asserting a bounded cap and disconnect.
