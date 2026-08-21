# Q1477: Rule matching order/precedence bug in Interface.sendMessageNow

## Question
Can an attacker choose an ICMP inner packet so `Interface.sendMessageNow` (inside.go) matches a permissive rule before a more specific deny, or short-circuits evaluation early?

## Target
- File/function: `inside.go` -> `Interface.sendMessageNow` (declared at inside.go:218)
- Entrypoint: Tunnel packet whose inner IP header and payload are fully attacker-authored
- Attacker controls: an ICMP inner packet; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Construct traffic that sits at the boundary between overlapping rules.
- Invariant to test: Rule evaluation is deterministic and a packet is allowed only if it matches an allow rule in full.
- Expected Immunefi impact: Firewall policy bypass for traffic the operator explicitly denied.
- Fast validation: Table-driven unit test over overlapping rule sets asserting `Interface.sendMessageNow` matches the intended rule.
