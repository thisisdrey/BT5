# Q1600: Unknown-value default-allow in tcpipChecksum

## Question
Does `tcpipChecksum` (iputil/packet.go) fall through to a permissive or default branch when an attacker supplies an inner protocol byte of 0xFF, instead of failing closed and dropping the packet?

## Target
- File/function: `iputil/packet.go` -> `tcpipChecksum` (declared at iputil/packet.go:485)
- Entrypoint: Unauthenticated UDP datagram delivered to the listener and dispatched through `Interface.readOutsidePackets`
- Attacker controls: an inner protocol byte of 0xFF; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Enumerate values outside the known enum/range and observe which branch of `tcpipChecksum` executes.
- Invariant to test: Unknown or reserved wire values are rejected, never mapped onto a default handler.
- Expected Immunefi impact: Authentication or firewall bypass reached by steering an unauthenticated packet into a handler it should never reach.
- Fast validation: Table-driven unit test over all 256 values of the field, asserting `tcpipChecksum` returns an error for every value not explicitly supported.
