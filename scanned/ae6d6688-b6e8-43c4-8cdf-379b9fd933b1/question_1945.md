# Q1945: Unbounded read/allocation in newDnsServerFromConfig

## Question
Can an oversized request line make `newDnsServerFromConfig` (dns_server.go) buffer or allocate without a size limit before the request is even parsed?

## Target
- File/function: `dns_server.go` -> `newDnsServerFromConfig` (declared at dns_server.go:60)
- Entrypoint: Unauthenticated TCP/UDP connection to a locally reachable Nebula listener (SSH admin or DNS)
- Attacker controls: an oversized request line; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send a request line that never terminates and measure retained memory.
- Invariant to test: Reads from an unauthenticated client are hard-bounded in size and time.
- Expected Immunefi impact: Memory exhaustion of the node from an unauthenticated local connection.
- Fast validation: Unit test streaming unbounded input to `newDnsServerFromConfig` and asserting a bounded cap and disconnect.
