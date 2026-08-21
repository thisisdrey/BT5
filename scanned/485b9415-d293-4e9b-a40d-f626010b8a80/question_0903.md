# Q0903: Write to tun without policy check in tun.activate6

## Question
Can an unsafe_route destination reach the tun write inside `tun.activate6` (overlay/tun_darwin.go) on a path that skips the firewall evaluation applied to the normal flow?

## Target
- File/function: `overlay/tun_darwin.go` -> `tun.activate6` (declared at overlay/tun_darwin.go:266)
- Entrypoint: Attacker-authored inner packet reaching the tun write path, or attacker-influenced route/MTU state
- Attacker controls: an unsafe_route destination; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Trace every caller reaching the write in `tun.activate6` and find one that does not evaluate the rule set first.
- Invariant to test: Every byte written to the tun device has passed firewall evaluation for the sending session.
- Expected Immunefi impact: Firewall bypass injecting attacker packets directly into the host's network stack.
- Fast validation: Integration test enumerating write callers and asserting a denied packet never appears on the tun device.
