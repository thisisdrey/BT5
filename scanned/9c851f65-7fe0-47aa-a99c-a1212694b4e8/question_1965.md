# Q1965: Out-of-bounds slice from attacker length in ringBuffer.Open

## Question
Can an unprivileged attacker send a single UDP datagram whose header.Subtype drives the slice bounds computed in `ringBuffer.Open` (udp/udp_rio_windows.go), so the parser indexes past the received buffer instead of rejecting the packet?

## Target
- File/function: `udp/udp_rio_windows.go` -> `ringBuffer.Open` (declared at udp/udp_rio_windows.go:402)
- Entrypoint: Unauthenticated UDP datagram delivered to the listener and dispatched through `Interface.readOutsidePackets`
- Attacker controls: header.Subtype; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Craft a datagram whose declared length/offset exceeds the bytes actually read, then observe the slice expression in `ringBuffer.Open` operate on the oversized bound.
- Invariant to test: Every offset and length derived from wire bytes is validated against the real datagram length before any slicing.
- Expected Immunefi impact: Remote node crash (panic) triggered by one unauthenticated packet, taking the tunnel down for all peers of that node.
- Fast validation: `go test -run Fuzz -fuzz` a harness feeding random byte slices into `ringBuffer.Open` and assert no panic and an error return for any input shorter than the declared length.
