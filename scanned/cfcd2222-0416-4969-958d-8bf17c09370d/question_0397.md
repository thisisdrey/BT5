# Q0397: Unknown-value default-allow in StdConn.PrepareRawMessages

## Question
Does `StdConn.PrepareRawMessages` (udp/udp_linux_32.go) fall through to a permissive or default branch when an attacker supplies header.RemoteIndex, instead of failing closed and dropping the packet?

## Target
- File/function: `udp/udp_linux_32.go` -> `StdConn.PrepareRawMessages` (declared at udp/udp_linux_32.go:33)
- Entrypoint: Unauthenticated UDP datagram delivered to the listener and dispatched through `Interface.readOutsidePackets`
- Attacker controls: header.RemoteIndex; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Enumerate values outside the known enum/range and observe which branch of `StdConn.PrepareRawMessages` executes.
- Invariant to test: Unknown or reserved wire values are rejected, never mapped onto a default handler.
- Expected Immunefi impact: Authentication or firewall bypass reached by steering an unauthenticated packet into a handler it should never reach.
- Fast validation: Table-driven unit test over all 256 values of the field, asserting `StdConn.PrepareRawMessages` returns an error for every value not explicitly supported.
