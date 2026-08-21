# Q3520: Error path leaves partial state in H.Parse

## Question
When `H.Parse` (header/header.go) errors out on an inner protocol byte of 0xFF, does it leave partially updated buffers, counters, or index entries that the next packet can observe?

## Target
- File/function: `header/header.go` -> `H.Parse` (declared at header/header.go:143)
- Entrypoint: Unauthenticated UDP datagram delivered to the listener and dispatched through `Interface.readOutsidePackets`
- Attacker controls: an inner protocol byte of 0xFF; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send an input that fails midway through `H.Parse`, then a valid packet, and compare behaviour to a clean node.
- Invariant to test: Every failure path in the receive loop restores the exact pre-packet state.
- Expected Immunefi impact: State corruption exploitable for tunnel wedging or cross-session data mixing.
- Fast validation: Unit test that runs `H.Parse` on failing input then valid input and asserts identical results to a fresh instance.
