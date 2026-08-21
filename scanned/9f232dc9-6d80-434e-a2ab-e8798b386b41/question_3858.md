# Q3858: Panic on device error path in Interface.Close

## Question
Can attacker-paced an MTU-sized packet forcing a split write drive `Interface.Close` (interface.go) into a device error path that panics or leaves the interface unusable?

## Target
- File/function: `interface.go` -> `Interface.Close` (declared at interface.go:555)
- Entrypoint: Attacker-authored inner packet reaching the tun write path, or attacker-influenced route/MTU state
- Attacker controls: an MTU-sized packet forcing a split write; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Force write failures under attacker-controlled load and observe recovery.
- Invariant to test: Device errors are handled without panic and the interface recovers or reports cleanly.
- Expected Immunefi impact: Persistent node outage triggered by remote traffic.
- Fast validation: Fault-injection test returning errors from the device write in `Interface.Close`, asserting no panic and continued operation.
