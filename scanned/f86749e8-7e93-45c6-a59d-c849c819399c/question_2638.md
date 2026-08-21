# Q2638: Query amplification through calculatedRemote.String

## Question
Can one small unauthenticated packet containing an advertised private/loopback remote make `calculatedRemote.String` (calculated_remote.go) emit a substantially larger or multiplied response?

## Target
- File/function: `calculated_remote.go` -> `calculatedRemote.String` (declared at calculated_remote.go:41)
- Entrypoint: Unauthenticated lighthouse/relay/handshake packet with attacker-chosen source address and advertised remotes
- Attacker controls: an advertised private/loopback remote; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Measure response bytes and packet count versus request size for `calculatedRemote.String`.
- Invariant to test: Response size and count are bounded relative to the request and gated on authentication.
- Expected Immunefi impact: Reflection/amplification DDoS using Nebula nodes against third parties.
- Fast validation: Benchmark test measuring the amplification factor of `calculatedRemote.String`, asserting it stays at or below 1.
