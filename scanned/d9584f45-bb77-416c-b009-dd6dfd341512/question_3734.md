# Q3734: Unknown-value default-allow in RIOConn.ListenOut

## Question
Does `RIOConn.ListenOut` (udp/udp_rio_windows.go) fall through to a permissive or default branch when an attacker supplies a 0-byte datagram, instead of failing closed and dropping the packet?

## Target
- File/function: `udp/udp_rio_windows.go` -> `RIOConn.ListenOut` (declared at udp/udp_rio_windows.go:143)
- Entrypoint: Unauthenticated UDP datagram delivered to the listener and dispatched through `Interface.readOutsidePackets`
- Attacker controls: a 0-byte datagram; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Enumerate values outside the known enum/range and observe which branch of `RIOConn.ListenOut` executes.
- Invariant to test: Unknown or reserved wire values are rejected, never mapped onto a default handler.
- Expected Immunefi impact: Authentication or firewall bypass reached by steering an unauthenticated packet into a handler it should never reach.
- Fast validation: Table-driven unit test over all 256 values of the field, asserting `RIOConn.ListenOut` returns an error for every value not explicitly supported.
