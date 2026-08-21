# Q0305: Rule matching order/precedence bug in NewConntrackCacheTicker

## Question
Can an attacker choose the destination port so `NewConntrackCacheTicker` (firewall/cache.go) matches a permissive rule before a more specific deny, or short-circuits evaluation early?

## Target
- File/function: `firewall/cache.go` -> `NewConntrackCacheTicker` (declared at firewall/cache.go:22)
- Entrypoint: Tunnel packet whose inner IP header and payload are fully attacker-authored
- Attacker controls: the destination port; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Construct traffic that sits at the boundary between overlapping rules.
- Invariant to test: Rule evaluation is deterministic and a packet is allowed only if it matches an allow rule in full.
- Expected Immunefi impact: Firewall policy bypass for traffic the operator explicitly denied.
- Fast validation: Table-driven unit test over overlapping rule sets asserting `NewConntrackCacheTicker` matches the intended rule.
