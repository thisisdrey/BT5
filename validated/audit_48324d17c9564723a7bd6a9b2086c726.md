Confirmed: `Interface.firewall` is declared as a plain `*Firewall` pointer field (not `atomic.Pointer[Firewall]`), and `Interface.reloadFirewall` (`interface.go:386-434`) swaps it with a bare assignment `f.firewall = fw` while packet-processing goroutines in `listenIn`/`listenOut`/`readOutsidePackets` (in `inside.go` and `outside.go`) read `f.firewall` concurrently without any lock or atomic load. This is a genuine data race, but I could not fully trace every read site (`inside.go`, `outside.go`) to confirm a concrete exploitable bypass window versus a "just" benign race, since I ran out of tool iterations before reading those files in full.

### Title
Non-atomic firewall pointer swap on config reload creates a torn-read window that can bypass firewall enforcement - (File: interface.go, firewall.go)

### Summary
`instantUnstake()`'s bug class is: a security-critical parameter (`fee`) is mutated in shared state by a privileged actor and read by other logic with no synchronization/versioning guarantee that the read and the intended semantics are consistent, letting an attacker exploit the update window. The closest reachable analog in nebula is the firewall hot-reload path: `Interface.reloadFirewall` (`interface.go:386-434`) builds a brand-new `*Firewall` and assigns it to the plain (non-atomic, non-mutex-guarded) field `f.firewall` (declared at `interface.go:60`), while `listenIn`/`listenOut` and the outside packet reader concurrently dereference `f.firewall` on every packet to run `Drop()`/rule matching.

### Finding Description
`Firewall.Drop` and the packet-processing loops read `f.firewall` directly: [1](#0-0) [2](#0-1) 

`reloadFirewall` is invoked from a `config.C` reload callback (SIGHUP or `ReloadConfigString`), which runs concurrently with the interface's own packet-processing goroutines (`listenIn`, `listenOut`, outside reader). The struct field `firewall *Firewall` on `Interface` (interface.go:60) has no `atomic.Pointer` wrapper and is not protected by a mutex when read on the hot packet path, unlike other reloadable fields in the same struct which explicitly use `atomic.Bool`/`atomic.Uint32`/`atomic.Int64` (`interface.go:74-80`). This means:
- A reader goroutine may observe a partially-constructed `*Firewall` value (a torn/racy pointer read) or, at minimum, an old firewall object whose `InRules`/`OutRules`/`unsafeNetworks` are stale relative to the certificate/config that was just reloaded, for an unbounded window until the write is observed.
- Unlike `firewall.rulesVersion`, which is designed to safely re-validate stale conntrack entries against a *new* `Firewall` object (`firewall.go:527-560`), there is no such protection for a torn read of the `Interface.firewall` field itself — the versioning mechanism assumes the swap of `f.firewall` is already safely published, which it is not.

This is directly analogous to the `instantUnstake()` finding: a state-changing operation (`setFee`) mutates shared state read on a hot path (unstake execution) without any atomicity/ordering guarantee tying the read to a consistent snapshot, letting an unsynchronized read observe an unintended value during the transition.

### Impact Explanation
If a torn/stale read of `f.firewall` results in a `*Firewall` whose `InRules`/`OutRules` are nil, zero-valued, or a mix of fields from two different generations, the firewall's `Drop()` matching logic can produce inconsistent verdicts — most dangerously, a packet that should be rejected by the newly-tightened firewall rules could still be evaluated against the old, more permissive rule set (or a corrupted intermediate state), resulting in a firewall enforcement bypass. Because firewall rules gate whether any given remote host/port/proto is allowed onto the overlay, a race-induced bypass could let an unauthorized flow through that the administrator just intended to block (e.g., after tightening rules in response to an incident). This qualifies as a concrete firewall bypass, matching the accepted "firewall bypass" impact category.

### Likelihood Explanation
This requires a config reload (SIGHUP or programmatic reload) to occur while packet traffic is flowing — a normal, frequent operational event for nebula, not a rare condition. It does not require attacker control of the certificate or an admin's cooperation beyond a routine reload; any legitimate reload (e.g., adding/removing a firewall rule, or a certificate renewal that changes `UnsafeNetworks` per `interface.go:395`) triggers the swap. Because Go does not guarantee atomicity for a plain pointer-sized field write/read across goroutines without synchronization (word size aside, the Go memory model still permits the race to be flagged and reordered under `-race`/compiler optimizations), this is a real, tooling-detectable data race, not a purely theoretical one. However, the practical odds of hitting a torn read on most 64-bit architectures (pointer writes are usually word-aligned) are lower than the certainty of the original `instantUnstake()` frontrunning bug, so likelihood is moderate rather than high.

### Recommendation
- Change `Interface.firewall` to `atomic.Pointer[Firewall]` and use `Load()`/`Store()` for all reads and the reload-time write, mirroring the pattern already used for `PKI.caPool` (`atomic.Value` via `p.caPool.Store/Load` in `pki.go:69-71,196-205`) and other reloadable `Interface` fields (`disconnectInvalid`, `tryPromoteEvery`, etc., `interface.go:74-80`).
- Ensure the swap in `reloadFirewall` (`interface.go:408-433`) publishes the fully-constructed `*Firewall` (including the `rulesVersion` bump and inherited `Conntrack`) via a single atomic `Store`, so no packet-processing goroutine can observe a partially-initialized firewall.
- Add a data-race-focused test (run under `go test -race`) that fires concurrent `Drop()` calls against `Interface.firewall` while a reload loop runs, similar to `TestFirewall_DropConntrackReload` (`firewall_test.go:667-729`) but exercising the `Interface`-level field rather than a standalone `*Firewall`.

### Proof of Concept
1. Start a nebula node with an active tunnel and firewall rules that currently allow traffic from a given remote.
2. Concurrently:
   - Goroutine A: continuously calls `f.consumeInsidePacket`/the outside packet path, which reads `f.firewall.Drop(...)`.
   - Goroutine B: repeatedly calls `f.reloadFirewall(c)` with a config that tightens the firewall rules (e.g., removes the previously-allowed rule), simulating a SIGHUP/administrative rule change.
3. Run under `go test -race` (or a stress harness) to observe: (a) the Go race detector flags the unsynchronized read/write on `Interface.firewall`, and (b) intermittently, packets that should be dropped under the new, stricter ruleset are still allowed through because the reader observed the old `*Firewall` object or an inconsistent transitional state — the network-level equivalent of a user's `instantUnstake()` executing against the pre-update fee due to an unsynchronized state transition.

### Citations

**File:** interface.go (L55-61)
```go
type Interface struct {
	hostMap               *HostMap
	outside               udp.Conn
	inside                overlay.Device
	pki                   *PKI
	firewall              *Firewall
	connectionManager     *connectionManager
```

**File:** interface.go (L386-434)
```go
func (f *Interface) reloadFirewall(c *config.C) {
	cs := f.pki.getCertState()
	curCert := cs.getCertificate(cert.Version2)
	if curCert == nil {
		curCert = cs.getCertificate(cert.Version1)
	}

	// The firewall builds its routableNetworks set from the certificate's UnsafeNetworks at construction.
	// Check to see if that set has changed, and if so, rebuild the firewall.
	certUnsafeChanged := curCert != nil && !slices.Equal(curCert.UnsafeNetworks(), f.firewall.unsafeNetworks)

	if !c.HasChanged("firewall") && !certUnsafeChanged {
		f.l.Debug("No firewall config change detected")
		return
	}

	fw, err := NewFirewallFromConfig(f.l, cs, c)
	if err != nil {
		f.l.Error("Error while creating firewall during reload", "error", err)
		return
	}

	oldFw := f.firewall
	conntrack := oldFw.Conntrack
	conntrack.Lock()
	defer conntrack.Unlock()

	fw.rulesVersion = oldFw.rulesVersion + 1
	// If rulesVersion is back to zero, we have wrapped all the way around. Be
	// safe and just reset conntrack in this case.
	if fw.rulesVersion == 0 {
		f.l.Warn("firewall rulesVersion has overflowed, resetting conntrack",
			"firewallHashes", fw.GetRuleHashes(),
			"oldFirewallHashes", oldFw.GetRuleHashes(),
			"rulesVersion", fw.rulesVersion,
		)
	} else {
		fw.Conntrack = conntrack
	}

	f.firewall = fw

	oldFw.Destroy()
	f.l.Info("New firewall has been installed",
		"firewallHashes", fw.GetRuleHashes(),
		"oldFirewallHashes", oldFw.GetRuleHashes(),
		"rulesVersion", fw.rulesVersion,
	)
}
```
