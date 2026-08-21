# Q2597: Unbounded pending-handshake growth via newCertStateFromConfig

## Question
Can an unprivileged attacker send a handshake naming a VPN address already owned to make `newCertStateFromConfig` (pki.go) accumulate pending handshake state without bound or eviction?

## Target
- File/function: `pki.go` -> `newCertStateFromConfig` (declared at pki.go:290)
- Entrypoint: Unauthenticated handshake packet (`header.Handshake`) from a host holding no CA-signed certificate
- Attacker controls: a handshake naming a VPN address already owned; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Emit many distinct handshake initiations from varied source addresses and measure retained state.
- Invariant to test: Pending handshake state is bounded and evicted by the timer wheel regardless of attacker input rate.
- Expected Immunefi impact: Memory exhaustion and handshake starvation for legitimate peers on the target node.
- Fast validation: Unit test driving N unique initiations through `newCertStateFromConfig` and asserting retained entries stay under the configured bound.
