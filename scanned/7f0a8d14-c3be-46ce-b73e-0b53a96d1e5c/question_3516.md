# Q3516: Unbounded pending-handshake growth via HandshakeManager.Complete

## Question
Can an unprivileged attacker send a handshake whose remote index collides with a live one to make `HandshakeManager.Complete` (handshake_manager.go) accumulate pending handshake state without bound or eviction?

## Target
- File/function: `handshake_manager.go` -> `HandshakeManager.Complete` (declared at handshake_manager.go:482)
- Entrypoint: Unauthenticated handshake packet (`header.Handshake`) from a host holding no CA-signed certificate
- Attacker controls: a handshake whose remote index collides with a live one; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Emit many distinct handshake initiations from varied source addresses and measure retained state.
- Invariant to test: Pending handshake state is bounded and evicted by the timer wheel regardless of attacker input rate.
- Expected Immunefi impact: Memory exhaustion and handshake starvation for legitimate peers on the target node.
- Fast validation: Unit test driving N unique initiations through `HandshakeManager.Complete` and asserting retained entries stay under the configured bound.
