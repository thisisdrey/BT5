# Q0129: Out-of-bounds slice from attacker length in StdConn.PrepareRawMessages

## Question
Can an unprivileged attacker send a single UDP datagram whose header.Type drives the slice bounds computed in `StdConn.PrepareRawMessages` (udp/udp_linux_32.go), so the parser indexes past the received buffer instead of rejecting the packet?

## Target
- File/function: `udp/udp_linux_32.go` -> `StdConn.PrepareRawMessages` (declared at udp/udp_linux_32.go:33)
- Entrypoint: Unauthenticated UDP datagram delivered to the listener and dispatched through `Interface.readOutsidePackets`
- Attacker controls: header.Type; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Craft a datagram whose declared length/offset exceeds the bytes actually read, then observe the slice expression in `StdConn.PrepareRawMessages` operate on the oversized bound.
- Invariant to test: Every offset and length derived from wire bytes is validated against the real datagram length before any slicing.
- Expected Immunefi impact: Remote node crash (panic) triggered by one unauthenticated packet, taking the tunnel down for all peers of that node.
- Fast validation: `go test -run Fuzz -fuzz` a harness feeding random byte slices into `StdConn.PrepareRawMessages` and assert no panic and an error return for any input shorter than the declared length.
