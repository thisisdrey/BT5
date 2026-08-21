# Q3425: VPN-address ownership check in connectionManager.makeTrafficDecision

## Question
Can an unprivileged attacker use a handshake naming a VPN address already owned so `connectionManager.makeTrafficDecision` (connection_manager.go) associates a VPN address with a session whose certificate does not list that address in its Networks?

## Target
- File/function: `connection_manager.go` -> `connectionManager.makeTrafficDecision` (declared at connection_manager.go:311)
- Entrypoint: Unauthenticated handshake packet (`header.Handshake`) from a host holding no CA-signed certificate
- Attacker controls: a handshake naming a VPN address already owned; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Handshake with a certificate for address A while claiming address B in the handshake details.
- Invariant to test: The VPN address bound to a session is always drawn from, and contained by, the verified certificate's Networks.
- Expected Immunefi impact: Overlay address takeover: attacker receives or intercepts traffic addressed to another host.
- Fast validation: Unit test on `connectionManager.makeTrafficDecision` with mismatched details/certificate networks, asserting rejection.
