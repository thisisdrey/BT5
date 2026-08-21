# Q3702: Error path leaves partial state in Interface.sendRecvError

## Question
When `Interface.sendRecvError` (outside.go) errors out on a truncated 15-byte datagram, does it leave partially updated buffers, counters, or index entries that the next packet can observe?

## Target
- File/function: `outside.go` -> `Interface.sendRecvError` (declared at outside.go:486)
- Entrypoint: Unauthenticated UDP datagram delivered to the listener and dispatched through `Interface.readOutsidePackets`
- Attacker controls: a truncated 15-byte datagram; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send an input that fails midway through `Interface.sendRecvError`, then a valid packet, and compare behaviour to a clean node.
- Invariant to test: Every failure path in the receive loop restores the exact pre-packet state.
- Expected Immunefi impact: State corruption exploitable for tunnel wedging or cross-session data mixing.
- Fast validation: Unit test that runs `Interface.sendRecvError` on failing input then valid input and asserts identical results to a fresh instance.
