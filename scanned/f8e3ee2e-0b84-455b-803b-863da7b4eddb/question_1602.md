# Q1602: Unknown-value default-allow in ipv6PseudoheaderChecksum

## Question
Does `ipv6PseudoheaderChecksum` (iputil/packet.go) fall through to a permissive or default branch when an attacker supplies header.Type, instead of failing closed and dropping the packet?

## Target
- File/function: `iputil/packet.go` -> `ipv6PseudoheaderChecksum` (declared at iputil/packet.go:520)
- Entrypoint: Unauthenticated UDP datagram delivered to the listener and dispatched through `Interface.readOutsidePackets`
- Attacker controls: header.Type; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Enumerate values outside the known enum/range and observe which branch of `ipv6PseudoheaderChecksum` executes.
- Invariant to test: Unknown or reserved wire values are rejected, never mapped onto a default handler.
- Expected Immunefi impact: Authentication or firewall bypass reached by steering an unauthenticated packet into a handler it should never reach.
- Fast validation: Table-driven unit test over all 256 values of the field, asserting `ipv6PseudoheaderChecksum` returns an error for every value not explicitly supported.
