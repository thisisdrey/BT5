# Q0174: Conntrack cache poisoning via Packet.MarshalJSON

## Question
Can an attacker use the destination port to create a conntrack entry in `Packet.MarshalJSON` (firewall/packet.go) that later authorizes traffic the rules would deny?

## Target
- File/function: `firewall/packet.go` -> `Packet.MarshalJSON` (declared at firewall/packet.go:45)
- Entrypoint: Tunnel packet whose inner IP header and payload are fully attacker-authored
- Attacker controls: the destination port; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send an initial allowed packet crafted so the cached tuple is broader than the rule that permitted it.
- Invariant to test: A cached flow authorizes only the exact tuple and identity that the original rule evaluation permitted.
- Expected Immunefi impact: Firewall bypass reaching inside services after a single permitted packet.
- Fast validation: Unit test creating an entry through `Packet.MarshalJSON` then probing adjacent tuples, asserting each is re-evaluated.
