# Q1637: Unauthenticated command reachable in dnsServer.parseQuery

## Question
Can a client that has not authenticated reach the handler logic in `dnsServer.parseQuery` (dns_server.go) by sending an unauthenticated command line?

## Target
- File/function: `dns_server.go` -> `dnsServer.parseQuery` (declared at dns_server.go:391)
- Entrypoint: Unauthenticated TCP/UDP connection to a locally reachable Nebula listener (SSH admin or DNS)
- Attacker controls: an unauthenticated command line; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Connect to the listener and issue the request before completing authentication.
- Invariant to test: Every state-changing or information-returning handler requires completed authentication.
- Expected Immunefi impact: Unauthenticated control of, or information disclosure from, a running Nebula node.
- Fast validation: Integration test issuing the request pre-auth against `dnsServer.parseQuery` and asserting refusal.
