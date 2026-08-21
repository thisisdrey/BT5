# Q1577: Conntrack cache poisoning via Firewall.Drop

## Question
Can an attacker use a conntrack-cached flow entry to create a conntrack entry in `Firewall.Drop` (firewall.go) that later authorizes traffic the rules would deny?

## Target
- File/function: `firewall.go` -> `Firewall.Drop` (declared at firewall.go:425)
- Entrypoint: Tunnel packet whose inner IP header and payload are fully attacker-authored
- Attacker controls: a conntrack-cached flow entry; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send an initial allowed packet crafted so the cached tuple is broader than the rule that permitted it.
- Invariant to test: A cached flow authorizes only the exact tuple and identity that the original rule evaluation permitted.
- Expected Immunefi impact: Firewall bypass reaching inside services after a single permitted packet.
- Fast validation: Unit test creating an entry through `Firewall.Drop` then probing adjacent tuples, asserting each is re-evaluated.
