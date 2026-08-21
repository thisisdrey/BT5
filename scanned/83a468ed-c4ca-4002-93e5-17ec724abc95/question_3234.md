# Q3234: Connection slot exhaustion via SSHServer.Stop

## Question
Can a flood of half-open connections exhaust the accept loop or goroutine budget in `SSHServer.Stop` (sshd/server.go) so legitimate use of the listener is blocked?

## Target
- File/function: `sshd/server.go` -> `SSHServer.Stop` (declared at sshd/server.go:266)
- Entrypoint: Unauthenticated TCP/UDP connection to a locally reachable Nebula listener (SSH admin or DNS)
- Attacker controls: a flood of half-open connections; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Open many connections that never complete a request.
- Invariant to test: Idle and half-open connections are timed out and total concurrency is bounded.
- Expected Immunefi impact: Denial of service against the node's control/DNS surface.
- Fast validation: Integration test opening N stalled connections against `SSHServer.Stop` and asserting a fresh client still succeeds.
