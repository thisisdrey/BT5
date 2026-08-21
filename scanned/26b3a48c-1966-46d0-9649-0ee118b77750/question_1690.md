# Q1690: Handshake timeout/retry abuse in ConnectionState.VerifyRelay

## Question
Can an attacker use a stage-2 handshake for an index never issued to keep `ConnectionState.VerifyRelay` (connection_state.go) retrying, re-sending, or holding a slot so a legitimate peer cannot complete its own handshake?

## Target
- File/function: `connection_state.go` -> `ConnectionState.VerifyRelay` (declared at connection_state.go:112)
- Entrypoint: Unauthenticated handshake packet (`header.Handshake`) from a host holding no CA-signed certificate
- Attacker controls: a stage-2 handshake for an index never issued; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Start handshakes that never complete and observe timer wheel and retry behaviour in `ConnectionState.VerifyRelay`.
- Invariant to test: Incomplete handshakes expire on a fixed schedule and never starve legitimate handshake capacity.
- Expected Immunefi impact: Denial of service preventing legitimate peers from establishing tunnels.
- Fast validation: Integration test with N stalled initiations plus one legitimate peer, asserting the legitimate handshake still completes.
