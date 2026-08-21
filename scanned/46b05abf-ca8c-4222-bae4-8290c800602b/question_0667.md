# Q0667: Conntrack cache poisoning via newAllowList

## Question
Can an attacker use the sending certificate's groups to create a conntrack entry in `newAllowList` (allow_list.go) that later authorizes traffic the rules would deny?

## Target
- File/function: `allow_list.go` -> `newAllowList` (declared at allow_list.go:84)
- Entrypoint: Tunnel packet whose inner IP header and payload are fully attacker-authored
- Attacker controls: the sending certificate's groups; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send an initial allowed packet crafted so the cached tuple is broader than the rule that permitted it.
- Invariant to test: A cached flow authorizes only the exact tuple and identity that the original rule evaluation permitted.
- Expected Immunefi impact: Firewall bypass reaching inside services after a single permitted packet.
- Fast validation: Unit test creating an entry through `newAllowList` then probing adjacent tuples, asserting each is re-evaluated.
