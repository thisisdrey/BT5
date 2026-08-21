# Q2251: Unknown-value default-allow in NewUDPStatsEmitter

## Question
Does `NewUDPStatsEmitter` (udp/udp_generic.go) fall through to a permissive or default branch when an attacker supplies an IPv6 packet with chained extension headers, instead of failing closed and dropping the packet?

## Target
- File/function: `udp/udp_generic.go` -> `NewUDPStatsEmitter` (declared at udp/udp_generic.go:67)
- Entrypoint: Unauthenticated UDP datagram delivered to the listener and dispatched through `Interface.readOutsidePackets`
- Attacker controls: an IPv6 packet with chained extension headers; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Enumerate values outside the known enum/range and observe which branch of `NewUDPStatsEmitter` executes.
- Invariant to test: Unknown or reserved wire values are rejected, never mapped onto a default handler.
- Expected Immunefi impact: Authentication or firewall bypass reached by steering an unauthenticated packet into a handler it should never reach.
- Fast validation: Table-driven unit test over all 256 values of the field, asserting `NewUDPStatsEmitter` returns an error for every value not explicitly supported.
