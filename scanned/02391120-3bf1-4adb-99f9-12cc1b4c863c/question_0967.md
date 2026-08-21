# Q0967: Counter/metric path diverges from decision in Packet.MarshalJSON

## Question
Does `Packet.MarshalJSON` (firewall/packet.go) drop a packet on the metric path while still forwarding it, or vice versa, when handling the inner protocol number?

## Target
- File/function: `firewall/packet.go` -> `Packet.MarshalJSON` (declared at firewall/packet.go:45)
- Entrypoint: Tunnel packet whose inner IP header and payload are fully attacker-authored
- Attacker controls: the inner protocol number; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Compare the accounted decision with the bytes actually written to the tun device.
- Invariant to test: The accounted decision and the executed action are always identical.
- Expected Immunefi impact: Silent firewall bypass invisible to operator monitoring.
- Fast validation: Integration test asserting per-packet metrics in `Packet.MarshalJSON` match tun writes exactly.
