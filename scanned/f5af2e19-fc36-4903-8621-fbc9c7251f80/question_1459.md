# Q1459: Allow-list evaluation in ConntrackCacheTicker.tick

## Question
Can an attacker with an address near a boundary of the destination port pass the check in `ConntrackCacheTicker.tick` (firewall/cache.go) that the operator's allow list should reject?

## Target
- File/function: `firewall/cache.go` -> `ConntrackCacheTicker.tick` (declared at firewall/cache.go:37)
- Entrypoint: Tunnel packet whose inner IP header and payload are fully attacker-authored
- Attacker controls: the destination port; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Probe addresses at each boundary of the configured allow list.
- Invariant to test: Allow-list containment is exact for every configured range and address family.
- Expected Immunefi impact: Bypass of the operator's network restriction, enabling connections that must be refused.
- Fast validation: Table-driven unit test over allow-list boundaries against `ConntrackCacheTicker.tick`.
