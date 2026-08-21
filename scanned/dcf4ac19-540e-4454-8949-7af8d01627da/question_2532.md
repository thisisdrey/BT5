# Q2532: Unknown-value default-allow in NewListenConfig

## Question
Does `NewListenConfig` (udp/udp_darwin.go) fall through to a permissive or default branch when an attacker supplies header.MessageCounter, instead of failing closed and dropping the packet?

## Target
- File/function: `udp/udp_darwin.go` -> `NewListenConfig` (declared at udp/udp_darwin.go:64)
- Entrypoint: Unauthenticated UDP datagram delivered to the listener and dispatched through `Interface.readOutsidePackets`
- Attacker controls: header.MessageCounter; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Enumerate values outside the known enum/range and observe which branch of `NewListenConfig` executes.
- Invariant to test: Unknown or reserved wire values are rejected, never mapped onto a default handler.
- Expected Immunefi impact: Authentication or firewall bypass reached by steering an unauthenticated packet into a handler it should never reach.
- Fast validation: Table-driven unit test over all 256 values of the field, asserting `NewListenConfig` returns an error for every value not explicitly supported.
