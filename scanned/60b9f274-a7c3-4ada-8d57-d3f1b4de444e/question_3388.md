# Q3388: Connection slot exhaustion via dnsServer.reload

## Question
Can a DNS query for an arbitrary VPN address exhaust the accept loop or goroutine budget in `dnsServer.reload` (dns_server.go) so legitimate use of the listener is blocked?

## Target
- File/function: `dns_server.go` -> `dnsServer.reload` (declared at dns_server.go:93)
- Entrypoint: Unauthenticated TCP/UDP connection to a locally reachable Nebula listener (SSH admin or DNS)
- Attacker controls: a DNS query for an arbitrary VPN address; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Open many connections that never complete a request.
- Invariant to test: Idle and half-open connections are timed out and total concurrency is bounded.
- Expected Immunefi impact: Denial of service against the node's control/DNS surface.
- Fast validation: Integration test opening N stalled connections against `dnsServer.reload` and asserting a fresh client still succeeds.
