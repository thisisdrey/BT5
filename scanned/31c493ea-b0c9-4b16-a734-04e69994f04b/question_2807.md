# Q2807: Error path leaves partial state in NewListener

## Question
When `NewListener` (udp/udp_bsd.go) errors out on a length field larger than the datagram, does it leave partially updated buffers, counters, or index entries that the next packet can observe?

## Target
- File/function: `udp/udp_bsd.go` -> `NewListener` (declared at udp/udp_bsd.go:19)
- Entrypoint: Unauthenticated UDP datagram delivered to the listener and dispatched through `Interface.readOutsidePackets`
- Attacker controls: a length field larger than the datagram; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send an input that fails midway through `NewListener`, then a valid packet, and compare behaviour to a clean node.
- Invariant to test: Every failure path in the receive loop restores the exact pre-packet state.
- Expected Immunefi impact: State corruption exploitable for tunnel wedging or cross-session data mixing.
- Fast validation: Unit test that runs `NewListener` on failing input then valid input and asserts identical results to a fresh instance.
