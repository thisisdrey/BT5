# Q2276: Unbounded pending-handshake growth via connectionManager.Start

## Question
Can an unprivileged attacker send a replayed stage-1 handshake to make `connectionManager.Start` (connection_manager.go) accumulate pending handshake state without bound or eviction?

## Target
- File/function: `connection_manager.go` -> `connectionManager.Start` (declared at connection_manager.go:139)
- Entrypoint: Unauthenticated handshake packet (`header.Handshake`) from a host holding no CA-signed certificate
- Attacker controls: a replayed stage-1 handshake; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Emit many distinct handshake initiations from varied source addresses and measure retained state.
- Invariant to test: Pending handshake state is bounded and evicted by the timer wheel regardless of attacker input rate.
- Expected Immunefi impact: Memory exhaustion and handshake starvation for legitimate peers on the target node.
- Fast validation: Unit test driving N unique initiations through `connectionManager.Start` and asserting retained entries stay under the configured bound.
