# Q3918: Handshake timeout/retry abuse in connectionManager.shouldSwapPrimary

## Question
Can an attacker use a handshake reusing a prior ephemeral key to keep `connectionManager.shouldSwapPrimary` (connection_manager.go) retrying, re-sending, or holding a slot so a legitimate peer cannot complete its own handshake?

## Target
- File/function: `connection_manager.go` -> `connectionManager.shouldSwapPrimary` (declared at connection_manager.go:444)
- Entrypoint: Unauthenticated handshake packet (`header.Handshake`) from a host holding no CA-signed certificate
- Attacker controls: a handshake reusing a prior ephemeral key; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Start handshakes that never complete and observe timer wheel and retry behaviour in `connectionManager.shouldSwapPrimary`.
- Invariant to test: Incomplete handshakes expire on a fixed schedule and never starve legitimate handshake capacity.
- Expected Immunefi impact: Denial of service preventing legitimate peers from establishing tunnels.
- Fast validation: Integration test with N stalled initiations plus one legitimate peer, asserting the legitimate handshake still completes.
