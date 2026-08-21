# Q2277: Unbounded pending-handshake growth via connectionManager.doTrafficCheck

## Question
Can an unprivileged attacker send a stage-2 handshake for an index never issued to make `connectionManager.doTrafficCheck` (connection_manager.go) accumulate pending handshake state without bound or eviction?

## Target
- File/function: `connection_manager.go` -> `connectionManager.doTrafficCheck` (declared at connection_manager.go:166)
- Entrypoint: Unauthenticated handshake packet (`header.Handshake`) from a host holding no CA-signed certificate
- Attacker controls: a stage-2 handshake for an index never issued; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Emit many distinct handshake initiations from varied source addresses and measure retained state.
- Invariant to test: Pending handshake state is bounded and evicted by the timer wheel regardless of attacker input rate.
- Expected Immunefi impact: Memory exhaustion and handshake starvation for legitimate peers on the target node.
- Fast validation: Unit test driving N unique initiations through `connectionManager.doTrafficCheck` and asserting retained entries stay under the configured bound.
