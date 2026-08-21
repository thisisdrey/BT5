# Q1455: Conntrack cache poisoning via Firewall.GetRuleHash

## Question
Can an attacker use the sending certificate's CA name/SHA to create a conntrack entry in `Firewall.GetRuleHash` (firewall.go) that later authorizes traffic the rules would deny?

## Target
- File/function: `firewall.go` -> `Firewall.GetRuleHash` (declared at firewall.go:302)
- Entrypoint: Tunnel packet whose inner IP header and payload are fully attacker-authored
- Attacker controls: the sending certificate's CA name/SHA; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send an initial allowed packet crafted so the cached tuple is broader than the rule that permitted it.
- Invariant to test: A cached flow authorizes only the exact tuple and identity that the original rule evaluation permitted.
- Expected Immunefi impact: Firewall bypass reaching inside services after a single permitted packet.
- Fast validation: Unit test creating an entry through `Firewall.GetRuleHash` then probing adjacent tuples, asserting each is re-evaluated.
