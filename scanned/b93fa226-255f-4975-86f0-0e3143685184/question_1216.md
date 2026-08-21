# Q1216: Local CIDR restriction bypass in NewConntrackCacheTicker

## Question
Can a conntrack-cached flow entry bypass the local_cidr restriction enforced in `NewConntrackCacheTicker` (firewall/cache.go), letting traffic reach a local destination the rule excludes?

## Target
- File/function: `firewall/cache.go` -> `NewConntrackCacheTicker` (declared at firewall/cache.go:22)
- Entrypoint: Tunnel packet whose inner IP header and payload are fully attacker-authored
- Attacker controls: a conntrack-cached flow entry; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Address traffic just outside/inside the local_cidr boundary and observe the decision.
- Invariant to test: local_cidr containment is checked on the actual write destination for every allowed packet.
- Expected Immunefi impact: Firewall bypass reaching restricted local destinations, including the host itself.
- Fast validation: Table-driven unit test over local_cidr boundaries asserting `NewConntrackCacheTicker` denies out-of-range destinations.
