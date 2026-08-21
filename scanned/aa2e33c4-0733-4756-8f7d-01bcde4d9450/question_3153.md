# Q3153: Unknown-value default-allow in StdConn.SupportsMultipleReaders

## Question
Does `StdConn.SupportsMultipleReaders` (udp/udp_darwin.go) fall through to a permissive or default branch when an attacker supplies header.Version set to an unknown value, instead of failing closed and dropping the packet?

## Target
- File/function: `udp/udp_darwin.go` -> `StdConn.SupportsMultipleReaders` (declared at udp/udp_darwin.go:186)
- Entrypoint: Unauthenticated UDP datagram delivered to the listener and dispatched through `Interface.readOutsidePackets`
- Attacker controls: header.Version set to an unknown value; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Enumerate values outside the known enum/range and observe which branch of `StdConn.SupportsMultipleReaders` executes.
- Invariant to test: Unknown or reserved wire values are rejected, never mapped onto a default handler.
- Expected Immunefi impact: Authentication or firewall bypass reached by steering an unauthenticated packet into a handler it should never reach.
- Fast validation: Table-driven unit test over all 256 values of the field, asserting `StdConn.SupportsMultipleReaders` returns an error for every value not explicitly supported.
