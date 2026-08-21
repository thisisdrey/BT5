# Q0597: Conntrack cache poisoning via Interface.consumeInsidePacket

## Question
Can an attacker use the inner protocol number to create a conntrack entry in `Interface.consumeInsidePacket` (inside.go) that later authorizes traffic the rules would deny?

## Target
- File/function: `inside.go` -> `Interface.consumeInsidePacket` (declared at inside.go:15)
- Entrypoint: Tunnel packet whose inner IP header and payload are fully attacker-authored
- Attacker controls: the inner protocol number; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send an initial allowed packet crafted so the cached tuple is broader than the rule that permitted it.
- Invariant to test: A cached flow authorizes only the exact tuple and identity that the original rule evaluation permitted.
- Expected Immunefi impact: Firewall bypass reaching inside services after a single permitted packet.
- Fast validation: Unit test creating an entry through `Interface.consumeInsidePacket` then probing adjacent tuples, asserting each is re-evaluated.
