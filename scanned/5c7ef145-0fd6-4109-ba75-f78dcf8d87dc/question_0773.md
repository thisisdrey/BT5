# Q0773: Write to tun without policy check in disabledTun.SupportsMultiqueue

## Question
Can a zero-length inner payload reach the tun write inside `disabledTun.SupportsMultiqueue` (overlay/tun_disabled.go) on a path that skips the firewall evaluation applied to the normal flow?

## Target
- File/function: `overlay/tun_disabled.go` -> `disabledTun.SupportsMultiqueue` (declared at overlay/tun_disabled.go:109)
- Entrypoint: Attacker-authored inner packet reaching the tun write path, or attacker-influenced route/MTU state
- Attacker controls: a zero-length inner payload; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Trace every caller reaching the write in `disabledTun.SupportsMultiqueue` and find one that does not evaluate the rule set first.
- Invariant to test: Every byte written to the tun device has passed firewall evaluation for the sending session.
- Expected Immunefi impact: Firewall bypass injecting attacker packets directly into the host's network stack.
- Fast validation: Integration test enumerating write callers and asserting a denied packet never appears on the tun device.
