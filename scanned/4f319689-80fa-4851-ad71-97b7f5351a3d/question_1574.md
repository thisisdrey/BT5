# Q1574: Handshake timeout/retry abuse in ConnectionState.MarshalJSON

## Question
Can an attacker use a handshake with a mismatched noise pattern/curve to keep `ConnectionState.MarshalJSON` (connection_state.go) retrying, re-sending, or holding a slot so a legitimate peer cannot complete its own handshake?

## Target
- File/function: `connection_state.go` -> `ConnectionState.MarshalJSON` (declared at connection_state.go:66)
- Entrypoint: Unauthenticated handshake packet (`header.Handshake`) from a host holding no CA-signed certificate
- Attacker controls: a handshake with a mismatched noise pattern/curve; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Start handshakes that never complete and observe timer wheel and retry behaviour in `ConnectionState.MarshalJSON`.
- Invariant to test: Incomplete handshakes expire on a fixed schedule and never starve legitimate handshake capacity.
- Expected Immunefi impact: Denial of service preventing legitimate peers from establishing tunnels.
- Fast validation: Integration test with N stalled initiations plus one legitimate peer, asserting the legitimate handshake still completes.
