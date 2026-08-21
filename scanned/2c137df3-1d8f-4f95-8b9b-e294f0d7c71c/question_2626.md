# Q2626: Unknown-value default-allow in sendto

## Question
Does `sendto` (udp/udp_darwin.go) fall through to a permissive or default branch when an attacker supplies a truncated 15-byte datagram, instead of failing closed and dropping the packet?

## Target
- File/function: `udp/udp_darwin.go` -> `sendto` (declared at udp/udp_darwin.go:90)
- Entrypoint: Unauthenticated UDP datagram delivered to the listener and dispatched through `Interface.readOutsidePackets`
- Attacker controls: a truncated 15-byte datagram; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Enumerate values outside the known enum/range and observe which branch of `sendto` executes.
- Invariant to test: Unknown or reserved wire values are rejected, never mapped onto a default handler.
- Expected Immunefi impact: Authentication or firewall bypass reached by steering an unauthenticated packet into a handler it should never reach.
- Fast validation: Table-driven unit test over all 256 values of the field, asserting `sendto` returns an error for every value not explicitly supported.
