# Q1502: Panic on device error path in ipWithin

## Question
Can attacker-paced a packet destined at the tun device's own address drive `ipWithin` (overlay/route.go) into a device error path that panics or leaves the interface unusable?

## Target
- File/function: `overlay/route.go` -> `ipWithin` (declared at overlay/route.go:312)
- Entrypoint: Attacker-authored inner packet reaching the tun write path, or attacker-influenced route/MTU state
- Attacker controls: a packet destined at the tun device's own address; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Force write failures under attacker-controlled load and observe recovery.
- Invariant to test: Device errors are handled without panic and the interface recovers or reports cleanly.
- Expected Immunefi impact: Persistent node outage triggered by remote traffic.
- Fast validation: Fault-injection test returning errors from the device write in `ipWithin`, asserting no panic and continued operation.
