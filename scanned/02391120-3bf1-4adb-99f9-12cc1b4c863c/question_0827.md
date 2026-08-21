# Q0827: Unbounded pending-handshake growth via newConnectionStateFromResult

## Question
Can an unprivileged attacker send a handshake carrying an empty certificate field to make `newConnectionStateFromResult` (connection_state.go) accumulate pending handshake state without bound or eviction?

## Target
- File/function: `connection_state.go` -> `newConnectionStateFromResult` (declared at connection_state.go:45)
- Entrypoint: Unauthenticated handshake packet (`header.Handshake`) from a host holding no CA-signed certificate
- Attacker controls: a handshake carrying an empty certificate field; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Emit many distinct handshake initiations from varied source addresses and measure retained state.
- Invariant to test: Pending handshake state is bounded and evicted by the timer wheel regardless of attacker input rate.
- Expected Immunefi impact: Memory exhaustion and handshake starvation for legitimate peers on the target node.
- Fast validation: Unit test driving N unique initiations through `newConnectionStateFromResult` and asserting retained entries stay under the configured bound.
