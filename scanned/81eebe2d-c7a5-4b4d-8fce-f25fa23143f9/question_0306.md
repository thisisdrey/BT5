# Q0306: Rule matching order/precedence bug in ConntrackCacheTicker.tick

## Question
Can an attacker choose a fragmented inner packet so `ConntrackCacheTicker.tick` (firewall/cache.go) matches a permissive rule before a more specific deny, or short-circuits evaluation early?

## Target
- File/function: `firewall/cache.go` -> `ConntrackCacheTicker.tick` (declared at firewall/cache.go:37)
- Entrypoint: Tunnel packet whose inner IP header and payload are fully attacker-authored
- Attacker controls: a fragmented inner packet; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Construct traffic that sits at the boundary between overlapping rules.
- Invariant to test: Rule evaluation is deterministic and a packet is allowed only if it matches an allow rule in full.
- Expected Immunefi impact: Firewall policy bypass for traffic the operator explicitly denied.
- Fast validation: Table-driven unit test over overlapping rule sets asserting `ConntrackCacheTicker.tick` matches the intended rule.
