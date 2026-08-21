# Q1339: Counter/metric path diverges from decision in ConntrackCacheTicker.Get

## Question
Does `ConntrackCacheTicker.Get` (firewall/cache.go) drop a packet on the metric path while still forwarding it, or vice versa, when handling the destination port?

## Target
- File/function: `firewall/cache.go` -> `ConntrackCacheTicker.Get` (declared at firewall/cache.go:52)
- Entrypoint: Tunnel packet whose inner IP header and payload are fully attacker-authored
- Attacker controls: the destination port; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Compare the accounted decision with the bytes actually written to the tun device.
- Invariant to test: The accounted decision and the executed action are always identical.
- Expected Immunefi impact: Silent firewall bypass invisible to operator monitoring.
- Fast validation: Integration test asserting per-packet metrics in `ConntrackCacheTicker.Get` match tun writes exactly.
