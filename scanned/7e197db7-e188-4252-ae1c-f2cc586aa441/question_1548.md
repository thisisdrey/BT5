# Q1548: Rule matching order/precedence bug in RemoteAllowList.getInsideAllowList

## Question
Can an attacker choose the sending certificate's groups so `RemoteAllowList.getInsideAllowList` (allow_list.go) matches a permissive rule before a more specific deny, or short-circuits evaluation early?

## Target
- File/function: `allow_list.go` -> `RemoteAllowList.getInsideAllowList` (declared at allow_list.go:298)
- Entrypoint: Tunnel packet whose inner IP header and payload are fully attacker-authored
- Attacker controls: the sending certificate's groups; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Construct traffic that sits at the boundary between overlapping rules.
- Invariant to test: Rule evaluation is deterministic and a packet is allowed only if it matches an allow rule in full.
- Expected Immunefi impact: Firewall policy bypass for traffic the operator explicitly denied.
- Fast validation: Table-driven unit test over overlapping rule sets asserting `RemoteAllowList.getInsideAllowList` matches the intended rule.
