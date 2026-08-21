# Q3739: Rule set reload race in getRemoteAllowRanges

## Question
Can an attacker time traffic against a firewall reload so `getRemoteAllowRanges` (allow_list.go) evaluates a fragmented inner packet against a partially installed rule set?

## Target
- File/function: `allow_list.go` -> `getRemoteAllowRanges` (declared at allow_list.go:210)
- Entrypoint: Tunnel packet whose inner IP header and payload are fully attacker-authored
- Attacker controls: a fragmented inner packet; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send continuous traffic while triggering a config reload and look for allowed packets during the swap.
- Invariant to test: Rule set swaps are atomic; no packet is ever evaluated against a partial rule set.
- Expected Immunefi impact: Transient full firewall bypass at every reload.
- Fast validation: `-race` test sending denied traffic in a loop during repeated reloads, asserting zero allows.
