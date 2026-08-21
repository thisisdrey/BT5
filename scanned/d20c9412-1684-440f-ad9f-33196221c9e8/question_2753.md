# Q2753: VPN-address ownership check in Machine.MessageIndex

## Question
Can an unprivileged attacker use a handshake arriving after the timer wheel expired the entry so `Machine.MessageIndex` (handshake/machine.go) associates a VPN address with a session whose certificate does not list that address in its Networks?

## Target
- File/function: `handshake/machine.go` -> `Machine.MessageIndex` (declared at handshake/machine.go:126)
- Entrypoint: Unauthenticated handshake packet (`header.Handshake`) from a host holding no CA-signed certificate
- Attacker controls: a handshake arriving after the timer wheel expired the entry; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Handshake with a certificate for address A while claiming address B in the handshake details.
- Invariant to test: The VPN address bound to a session is always drawn from, and contained by, the verified certificate's Networks.
- Expected Immunefi impact: Overlay address takeover: attacker receives or intercepts traffic addressed to another host.
- Fast validation: Unit test on `Machine.MessageIndex` with mismatched details/certificate networks, asserting rejection.
