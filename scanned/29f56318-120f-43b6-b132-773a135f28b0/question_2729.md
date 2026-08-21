# Q2729: Query amplification through calculatedRemote.ApplyV6

## Question
Can one small unauthenticated packet containing a relay request for a host it does not own make `calculatedRemote.ApplyV6` (calculated_remote.go) emit a substantially larger or multiplied response?

## Target
- File/function: `calculated_remote.go` -> `calculatedRemote.ApplyV6` (declared at calculated_remote.go:59)
- Entrypoint: Unauthenticated lighthouse/relay/handshake packet with attacker-chosen source address and advertised remotes
- Attacker controls: a relay request for a host it does not own; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Measure response bytes and packet count versus request size for `calculatedRemote.ApplyV6`.
- Invariant to test: Response size and count are bounded relative to the request and gated on authentication.
- Expected Immunefi impact: Reflection/amplification DDoS using Nebula nodes against third parties.
- Fast validation: Benchmark test measuring the amplification factor of `calculatedRemote.ApplyV6`, asserting it stays at or below 1.
