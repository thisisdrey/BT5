# Q2244: Connection slot exhaustion via allCommands

## Question
Can an unauthenticated command line exhaust the accept loop or goroutine budget in `allCommands` (sshd/command.go) so legitimate use of the listener is blocked?

## Target
- File/function: `sshd/command.go` -> `allCommands` (declared at sshd/command.go:96)
- Entrypoint: Unauthenticated TCP/UDP connection to a locally reachable Nebula listener (SSH admin or DNS)
- Attacker controls: an unauthenticated command line; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Open many connections that never complete a request.
- Invariant to test: Idle and half-open connections are timed out and total concurrency is bounded.
- Expected Immunefi impact: Denial of service against the node's control/DNS surface.
- Fast validation: Integration test opening N stalled connections against `allCommands` and asserting a fresh client still succeeds.
