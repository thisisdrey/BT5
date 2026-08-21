# Q0859: Conntrack cache poisoning via Interface.sendMessageNow

## Question
Can an attacker use a localCIDR-restricted rule to create a conntrack entry in `Interface.sendMessageNow` (inside.go) that later authorizes traffic the rules would deny?

## Target
- File/function: `inside.go` -> `Interface.sendMessageNow` (declared at inside.go:218)
- Entrypoint: Tunnel packet whose inner IP header and payload are fully attacker-authored
- Attacker controls: a localCIDR-restricted rule; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send an initial allowed packet crafted so the cached tuple is broader than the rule that permitted it.
- Invariant to test: A cached flow authorizes only the exact tuple and identity that the original rule evaluation permitted.
- Expected Immunefi impact: Firewall bypass reaching inside services after a single permitted packet.
- Fast validation: Unit test creating an entry through `Interface.sendMessageNow` then probing adjacent tuples, asserting each is re-evaluated.
