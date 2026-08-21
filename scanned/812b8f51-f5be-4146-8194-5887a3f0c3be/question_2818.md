# Q2818: Query amplification through newCalculatedRemotesListFromConfig

## Question
Can one small unauthenticated packet containing a HostUpdateNotification for another host's address make `newCalculatedRemotesListFromConfig` (calculated_remote.go) emit a substantially larger or multiplied response?

## Target
- File/function: `calculated_remote.go` -> `newCalculatedRemotesListFromConfig` (declared at calculated_remote.go:108)
- Entrypoint: Unauthenticated lighthouse/relay/handshake packet with attacker-chosen source address and advertised remotes
- Attacker controls: a HostUpdateNotification for another host's address; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Measure response bytes and packet count versus request size for `newCalculatedRemotesListFromConfig`.
- Invariant to test: Response size and count are bounded relative to the request and gated on authentication.
- Expected Immunefi impact: Reflection/amplification DDoS using Nebula nodes against third parties.
- Fast validation: Benchmark test measuring the amplification factor of `newCalculatedRemotesListFromConfig`, asserting it stays at or below 1.
