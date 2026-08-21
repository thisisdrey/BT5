# Q1426: Rule matching order/precedence bug in LocalAllowList.AllowName

## Question
Can an attacker choose the inner source VPN address so `LocalAllowList.AllowName` (allow_list.go) matches a permissive rule before a more specific deny, or short-circuits evaluation early?

## Target
- File/function: `allow_list.go` -> `LocalAllowList.AllowName` (declared at allow_list.go:255)
- Entrypoint: Tunnel packet whose inner IP header and payload are fully attacker-authored
- Attacker controls: the inner source VPN address; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Construct traffic that sits at the boundary between overlapping rules.
- Invariant to test: Rule evaluation is deterministic and a packet is allowed only if it matches an allow rule in full.
- Expected Immunefi impact: Firewall policy bypass for traffic the operator explicitly denied.
- Fast validation: Table-driven unit test over overlapping rule sets asserting `LocalAllowList.AllowName` matches the intended rule.
