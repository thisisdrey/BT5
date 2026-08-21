# Q3183: CIDR/prefix boundary handling in NewFirewallFromConfig

## Question
Does `NewFirewallFromConfig` (firewall.go) mishandle prefix boundaries for the sending certificate's CA name/SHA, for example /0, /31, /32, or a mapped IPv4-in-IPv6 address?

## Target
- File/function: `firewall.go` -> `NewFirewallFromConfig` (declared at firewall.go:195)
- Entrypoint: Tunnel packet whose inner IP header and payload are fully attacker-authored
- Attacker controls: the sending certificate's CA name/SHA; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send traffic whose address sits exactly at a prefix edge or in a mapped-address form.
- Invariant to test: Prefix containment is exact for every prefix length and address family, with mapped forms normalized once.
- Expected Immunefi impact: Firewall bypass or address-scope escape allowing traffic to a denied inside range.
- Fast validation: Table-driven unit test over boundary prefixes and mapped addresses against `NewFirewallFromConfig`.
