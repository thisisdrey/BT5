# Q1121: Unknown-value default-allow in CreateRejectPacket

## Question
Does `CreateRejectPacket` (iputil/packet.go) fall through to a permissive or default branch when an attacker supplies header.RemoteIndex, instead of failing closed and dropping the packet?

## Target
- File/function: `iputil/packet.go` -> `CreateRejectPacket` (declared at iputil/packet.go:32)
- Entrypoint: Unauthenticated UDP datagram delivered to the listener and dispatched through `Interface.readOutsidePackets`
- Attacker controls: header.RemoteIndex; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Enumerate values outside the known enum/range and observe which branch of `CreateRejectPacket` executes.
- Invariant to test: Unknown or reserved wire values are rejected, never mapped onto a default handler.
- Expected Immunefi impact: Authentication or firewall bypass reached by steering an unauthenticated packet into a handler it should never reach.
- Fast validation: Table-driven unit test over all 256 values of the field, asserting `CreateRejectPacket` returns an error for every value not explicitly supported.
