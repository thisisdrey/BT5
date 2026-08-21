# Q2843: VPN-address ownership check in Machine.peerMsgFlags

## Question
Can an unprivileged attacker use a stage-2 handshake for an index never issued so `Machine.peerMsgFlags` (handshake/machine.go) associates a VPN address with a session whose certificate does not list that address in its Networks?

## Target
- File/function: `handshake/machine.go` -> `Machine.peerMsgFlags` (declared at handshake/machine.go:150)
- Entrypoint: Unauthenticated handshake packet (`header.Handshake`) from a host holding no CA-signed certificate
- Attacker controls: a stage-2 handshake for an index never issued; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Handshake with a certificate for address A while claiming address B in the handshake details.
- Invariant to test: The VPN address bound to a session is always drawn from, and contained by, the verified certificate's Networks.
- Expected Immunefi impact: Overlay address takeover: attacker receives or intercepts traffic addressed to another host.
- Fast validation: Unit test on `Machine.peerMsgFlags` with mismatched details/certificate networks, asserting rejection.
