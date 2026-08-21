# Q3898: Unknown-value default-allow in RIOConn.receive

## Question
Does `RIOConn.receive` (udp/udp_rio_windows.go) fall through to a permissive or default branch when an attacker supplies an IPv4 packet with IHL=15 and no options, instead of failing closed and dropping the packet?

## Target
- File/function: `udp/udp_rio_windows.go` -> `RIOConn.receive` (declared at udp/udp_rio_windows.go:184)
- Entrypoint: Unauthenticated UDP datagram delivered to the listener and dispatched through `Interface.readOutsidePackets`
- Attacker controls: an IPv4 packet with IHL=15 and no options; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Enumerate values outside the known enum/range and observe which branch of `RIOConn.receive` executes.
- Invariant to test: Unknown or reserved wire values are rejected, never mapped onto a default handler.
- Expected Immunefi impact: Authentication or firewall bypass reached by steering an unauthenticated packet into a handler it should never reach.
- Fast validation: Table-driven unit test over all 256 values of the field, asserting `RIOConn.receive` returns an error for every value not explicitly supported.
