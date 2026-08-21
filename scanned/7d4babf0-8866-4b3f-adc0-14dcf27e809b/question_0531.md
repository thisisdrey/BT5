# Q0531: Out-of-bounds slice from attacker length in RIOConn.ListenOut

## Question
Can an unprivileged attacker send a single UDP datagram whose header.MessageCounter drives the slice bounds computed in `RIOConn.ListenOut` (udp/udp_rio_windows.go), so the parser indexes past the received buffer instead of rejecting the packet?

## Target
- File/function: `udp/udp_rio_windows.go` -> `RIOConn.ListenOut` (declared at udp/udp_rio_windows.go:143)
- Entrypoint: Unauthenticated UDP datagram delivered to the listener and dispatched through `Interface.readOutsidePackets`
- Attacker controls: header.MessageCounter; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Craft a datagram whose declared length/offset exceeds the bytes actually read, then observe the slice expression in `RIOConn.ListenOut` operate on the oversized bound.
- Invariant to test: Every offset and length derived from wire bytes is validated against the real datagram length before any slicing.
- Expected Immunefi impact: Remote node crash (panic) triggered by one unauthenticated packet, taking the tunnel down for all peers of that node.
- Fast validation: `go test -run Fuzz -fuzz` a harness feeding random byte slices into `RIOConn.ListenOut` and assert no panic and an error return for any input shorter than the declared length.
