# Q0799: Conntrack cache poisoning via LocalAllowList.Allow

## Question
Can an attacker use an ICMP inner packet to create a conntrack entry in `LocalAllowList.Allow` (allow_list.go) that later authorizes traffic the rules would deny?

## Target
- File/function: `allow_list.go` -> `LocalAllowList.Allow` (declared at allow_list.go:248)
- Entrypoint: Tunnel packet whose inner IP header and payload are fully attacker-authored
- Attacker controls: an ICMP inner packet; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send an initial allowed packet crafted so the cached tuple is broader than the rule that permitted it.
- Invariant to test: A cached flow authorizes only the exact tuple and identity that the original rule evaluation permitted.
- Expected Immunefi impact: Firewall bypass reaching inside services after a single permitted packet.
- Fast validation: Unit test creating an entry through `LocalAllowList.Allow` then probing adjacent tuples, asserting each is re-evaluated.
