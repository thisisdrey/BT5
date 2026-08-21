# Q2030: Panic on device error path in NameError.Error

## Question
Can attacker-paced a zero-length inner payload drive `NameError.Error` (overlay/tun.go) into a device error path that panics or leaves the interface unusable?

## Target
- File/function: `overlay/tun.go` -> `NameError.Error` (declared at overlay/tun.go:20)
- Entrypoint: Attacker-authored inner packet reaching the tun write path, or attacker-influenced route/MTU state
- Attacker controls: a zero-length inner payload; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Force write failures under attacker-controlled load and observe recovery.
- Invariant to test: Device errors are handled without panic and the interface recovers or reports cleanly.
- Expected Immunefi impact: Persistent node outage triggered by remote traffic.
- Fast validation: Fault-injection test returning errors from the device write in `NameError.Error`, asserting no panic and continued operation.
