# Q1460: Allow-list evaluation in ConntrackCacheTicker.Get

## Question
Can an attacker with an address near a boundary of a fragmented inner packet pass the check in `ConntrackCacheTicker.Get` (firewall/cache.go) that the operator's allow list should reject?

## Target
- File/function: `firewall/cache.go` -> `ConntrackCacheTicker.Get` (declared at firewall/cache.go:52)
- Entrypoint: Tunnel packet whose inner IP header and payload are fully attacker-authored
- Attacker controls: a fragmented inner packet; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Probe addresses at each boundary of the configured allow list.
- Invariant to test: Allow-list containment is exact for every configured range and address family.
- Expected Immunefi impact: Bypass of the operator's network restriction, enabling connections that must be refused.
- Fast validation: Table-driven unit test over allow-list boundaries against `ConntrackCacheTicker.Get`.
