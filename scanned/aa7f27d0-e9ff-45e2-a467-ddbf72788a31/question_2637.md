# Q2637: Query amplification through newCalculatedRemote

## Question
Can one small unauthenticated packet containing a spoofed UDP source address make `newCalculatedRemote` (calculated_remote.go) emit a substantially larger or multiplied response?

## Target
- File/function: `calculated_remote.go` -> `newCalculatedRemote` (declared at calculated_remote.go:24)
- Entrypoint: Unauthenticated lighthouse/relay/handshake packet with attacker-chosen source address and advertised remotes
- Attacker controls: a spoofed UDP source address; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Measure response bytes and packet count versus request size for `newCalculatedRemote`.
- Invariant to test: Response size and count are bounded relative to the request and gated on authentication.
- Expected Immunefi impact: Reflection/amplification DDoS using Nebula nodes against third parties.
- Fast validation: Benchmark test measuring the amplification factor of `newCalculatedRemote`, asserting it stays at or below 1.
