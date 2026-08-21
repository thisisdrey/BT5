# Q1094: Rule set reload race in ConntrackCacheTicker.tick

## Question
Can an attacker time traffic against a firewall reload so `ConntrackCacheTicker.tick` (firewall/cache.go) evaluates a conntrack-cached flow entry against a partially installed rule set?

## Target
- File/function: `firewall/cache.go` -> `ConntrackCacheTicker.tick` (declared at firewall/cache.go:37)
- Entrypoint: Tunnel packet whose inner IP header and payload are fully attacker-authored
- Attacker controls: a conntrack-cached flow entry; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send continuous traffic while triggering a config reload and look for allowed packets during the swap.
- Invariant to test: Rule set swaps are atomic; no packet is ever evaluated against a partial rule set.
- Expected Immunefi impact: Transient full firewall bypass at every reload.
- Fast validation: `-race` test sending denied traffic in a loop during repeated reloads, asserting zero allows.
