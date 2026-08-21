# Q2055: Connection slot exhaustion via lookupCommand

## Question
Can a partially written request left open exhaust the accept loop or goroutine budget in `lookupCommand` (sshd/command.go) so legitimate use of the listener is blocked?

## Target
- File/function: `sshd/command.go` -> `lookupCommand` (declared at sshd/command.go:72)
- Entrypoint: Unauthenticated TCP/UDP connection to a locally reachable Nebula listener (SSH admin or DNS)
- Attacker controls: a partially written request left open; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Open many connections that never complete a request.
- Invariant to test: Idle and half-open connections are timed out and total concurrency is bounded.
- Expected Immunefi impact: Denial of service against the node's control/DNS surface.
- Fast validation: Integration test opening N stalled connections against `lookupCommand` and asserting a fresh client still succeeds.
