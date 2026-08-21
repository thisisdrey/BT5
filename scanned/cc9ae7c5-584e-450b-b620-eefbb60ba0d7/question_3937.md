# Q3937: Rule set reload race in Interface.getOrHandshakeNoRouting

## Question
Can an attacker time traffic against a firewall reload so `Interface.getOrHandshakeNoRouting` (inside.go) evaluates the destination port against a partially installed rule set?

## Target
- File/function: `inside.go` -> `Interface.getOrHandshakeNoRouting` (declared at inside.go:136)
- Entrypoint: Tunnel packet whose inner IP header and payload are fully attacker-authored
- Attacker controls: the destination port; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send continuous traffic while triggering a config reload and look for allowed packets during the swap.
- Invariant to test: Rule set swaps are atomic; no packet is ever evaluated against a partial rule set.
- Expected Immunefi impact: Transient full firewall bypass at every reload.
- Fast validation: `-race` test sending denied traffic in a loop during repeated reloads, asserting zero allows.
