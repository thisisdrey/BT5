# Q0645: Write to tun without policy check in winTun.addRoutes

## Question
Can a multicast/broadcast inner destination reach the tun write inside `winTun.addRoutes` (overlay/tun_windows.go) on a path that skips the firewall evaluation applied to the normal flow?

## Target
- File/function: `overlay/tun_windows.go` -> `winTun.addRoutes` (declared at overlay/tun_windows.go:177)
- Entrypoint: Attacker-authored inner packet reaching the tun write path, or attacker-influenced route/MTU state
- Attacker controls: a multicast/broadcast inner destination; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Trace every caller reaching the write in `winTun.addRoutes` and find one that does not evaluate the rule set first.
- Invariant to test: Every byte written to the tun device has passed firewall evaluation for the sending session.
- Expected Immunefi impact: Firewall bypass injecting attacker packets directly into the host's network stack.
- Fast validation: Integration test enumerating write callers and asserting a denied packet never appears on the tun device.
