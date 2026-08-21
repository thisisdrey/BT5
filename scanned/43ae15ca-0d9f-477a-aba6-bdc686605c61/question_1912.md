# Q1912: CIDR/prefix boundary handling in Interface.getOrHandshakeNoRouting

## Question
Does `Interface.getOrHandshakeNoRouting` (inside.go) mishandle prefix boundaries for a localCIDR-restricted rule, for example /0, /31, /32, or a mapped IPv4-in-IPv6 address?

## Target
- File/function: `inside.go` -> `Interface.getOrHandshakeNoRouting` (declared at inside.go:136)
- Entrypoint: Tunnel packet whose inner IP header and payload are fully attacker-authored
- Attacker controls: a localCIDR-restricted rule; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send traffic whose address sits exactly at a prefix edge or in a mapped-address form.
- Invariant to test: Prefix containment is exact for every prefix length and address family, with mapped forms normalized once.
- Expected Immunefi impact: Firewall bypass or address-scope escape allowing traffic to a denied inside range.
- Fast validation: Table-driven unit test over boundary prefixes and mapped addresses against `Interface.getOrHandshakeNoRouting`.
