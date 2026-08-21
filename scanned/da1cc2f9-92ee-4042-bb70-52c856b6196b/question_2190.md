# Q2190: Unbounded pending-handshake growth via Machine.marshalOutgoing

## Question
Can an unprivileged attacker send a burst of handshakes from one source address to make `Machine.marshalOutgoing` (handshake/machine.go) accumulate pending handshake state without bound or eviction?

## Target
- File/function: `handshake/machine.go` -> `Machine.marshalOutgoing` (declared at handshake/machine.go:382)
- Entrypoint: Unauthenticated handshake packet (`header.Handshake`) from a host holding no CA-signed certificate
- Attacker controls: a burst of handshakes from one source address; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Emit many distinct handshake initiations from varied source addresses and measure retained state.
- Invariant to test: Pending handshake state is bounded and evicted by the timer wheel regardless of attacker input rate.
- Expected Immunefi impact: Memory exhaustion and handshake starvation for legitimate peers on the target node.
- Fast validation: Unit test driving N unique initiations through `Machine.marshalOutgoing` and asserting retained entries stay under the configured bound.
