# Q3306: Connection slot exhaustion via newDnsServerFromConfig

## Question
Can a malformed DNS TXT query exhaust the accept loop or goroutine budget in `newDnsServerFromConfig` (dns_server.go) so legitimate use of the listener is blocked?

## Target
- File/function: `dns_server.go` -> `newDnsServerFromConfig` (declared at dns_server.go:60)
- Entrypoint: Unauthenticated TCP/UDP connection to a locally reachable Nebula listener (SSH admin or DNS)
- Attacker controls: a malformed DNS TXT query; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Open many connections that never complete a request.
- Invariant to test: Idle and half-open connections are timed out and total concurrency is bounded.
- Expected Immunefi impact: Denial of service against the node's control/DNS surface.
- Fast validation: Integration test opening N stalled connections against `newDnsServerFromConfig` and asserting a fresh client still succeeds.
