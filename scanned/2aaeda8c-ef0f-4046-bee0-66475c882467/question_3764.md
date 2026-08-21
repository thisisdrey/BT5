# Q3764: Unbounded pending-handshake growth via HandshakeManager.EmitStats

## Question
Can an unprivileged attacker send a handshake carrying an empty certificate field to make `HandshakeManager.EmitStats` (handshake_manager.go) accumulate pending handshake state without bound or eviction?

## Target
- File/function: `handshake_manager.go` -> `HandshakeManager.EmitStats` (declared at handshake_manager.go:611)
- Entrypoint: Unauthenticated handshake packet (`header.Handshake`) from a host holding no CA-signed certificate
- Attacker controls: a handshake carrying an empty certificate field; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Emit many distinct handshake initiations from varied source addresses and measure retained state.
- Invariant to test: Pending handshake state is bounded and evicted by the timer wheel regardless of attacker input rate.
- Expected Immunefi impact: Memory exhaustion and handshake starvation for legitimate peers on the target node.
- Fast validation: Unit test driving N unique initiations through `HandshakeManager.EmitStats` and asserting retained entries stay under the configured bound.
