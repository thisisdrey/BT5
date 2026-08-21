# Q0968: Allow-list evaluation in Packet.Copy

## Question
Can an attacker with an address near a boundary of the inner protocol number pass the check in `Packet.Copy` (firewall/packet.go) that the operator's allow list should reject?

## Target
- File/function: `firewall/packet.go` -> `Packet.Copy` (declared at firewall/packet.go:34)
- Entrypoint: Tunnel packet whose inner IP header and payload are fully attacker-authored
- Attacker controls: the inner protocol number; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Probe addresses at each boundary of the configured allow list.
- Invariant to test: Allow-list containment is exact for every configured range and address family.
- Expected Immunefi impact: Bypass of the operator's network restriction, enabling connections that must be refused.
- Fast validation: Table-driven unit test over allow-list boundaries against `Packet.Copy`.
