# Q1484: Unknown-value default-allow in createICMPv6EchoResponse

## Question
Does `createICMPv6EchoResponse` (iputil/packet.go) fall through to a permissive or default branch when an attacker supplies a message type outside the known enum, instead of failing closed and dropping the packet?

## Target
- File/function: `iputil/packet.go` -> `createICMPv6EchoResponse` (declared at iputil/packet.go:443)
- Entrypoint: Unauthenticated UDP datagram delivered to the listener and dispatched through `Interface.readOutsidePackets`
- Attacker controls: a message type outside the known enum; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Enumerate values outside the known enum/range and observe which branch of `createICMPv6EchoResponse` executes.
- Invariant to test: Unknown or reserved wire values are rejected, never mapped onto a default handler.
- Expected Immunefi impact: Authentication or firewall bypass reached by steering an unauthenticated packet into a handler it should never reach.
- Fast validation: Table-driven unit test over all 256 values of the field, asserting `createICMPv6EchoResponse` returns an error for every value not explicitly supported.
