# Q1453: VPN-address ownership check in ConnectionState.Decrypt

## Question
Can an unprivileged attacker use a handshake reusing a prior ephemeral key so `ConnectionState.Decrypt` (connection_state.go) associates a VPN address with a session whose certificate does not list that address in its Networks?

## Target
- File/function: `connection_state.go` -> `ConnectionState.Decrypt` (declared at connection_state.go:88)
- Entrypoint: Unauthenticated handshake packet (`header.Handshake`) from a host holding no CA-signed certificate
- Attacker controls: a handshake reusing a prior ephemeral key; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Handshake with a certificate for address A while claiming address B in the handshake details.
- Invariant to test: The VPN address bound to a session is always drawn from, and contained by, the verified certificate's Networks.
- Expected Immunefi impact: Overlay address takeover: attacker receives or intercepts traffic addressed to another host.
- Fast validation: Unit test on `ConnectionState.Decrypt` with mismatched details/certificate networks, asserting rejection.
