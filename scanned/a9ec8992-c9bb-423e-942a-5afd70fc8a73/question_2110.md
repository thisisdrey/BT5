# Q2110: CIDR/prefix boundary handling in Interface.dropExhausted

## Question
Does `Interface.dropExhausted` (inside.go) mishandle prefix boundaries for the sending certificate's groups, for example /0, /31, /32, or a mapped IPv4-in-IPv6 address?

## Target
- File/function: `inside.go` -> `Interface.dropExhausted` (declared at inside.go:279)
- Entrypoint: Tunnel packet whose inner IP header and payload are fully attacker-authored
- Attacker controls: the sending certificate's groups; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send traffic whose address sits exactly at a prefix edge or in a mapped-address form.
- Invariant to test: Prefix containment is exact for every prefix length and address family, with mapped forms normalized once.
- Expected Immunefi impact: Firewall bypass or address-scope escape allowing traffic to a denied inside range.
- Fast validation: Table-driven unit test over boundary prefixes and mapped addresses against `Interface.dropExhausted`.
