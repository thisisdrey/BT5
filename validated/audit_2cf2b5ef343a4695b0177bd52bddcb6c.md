### Title
Unsynchronized firewall pointer swap during config reload allows a race-window firewall/authorization bypass - ([File: interface.go])

### Summary
`Interface.firewall` is a bare, unsynchronized `*Firewall` field that is both read by every packet-processing goroutine (tun and UDP listener routines) on every inbound/outbound packet and written wholesale by `reloadFirewall()` when the operator changes firewall rules or the host certificate's `UnsafeNetworks` set changes. Unlike the certificate/CA-pool state (`p.cs` / `p.caPool`, which use `atomic.Value.Store/Load`), the firewall pointer is swapped with a plain assignment with no `atomic.Pointer`/mutex protecting concurrent readers. This is the same class of bug as the ERC20 `approve`/`transferFrom` race: a legitimate state-changing operation ("Alice" reloading the firewall/tightening rules) races against concurrently-in-flight consumers ("Bob"'s packets), and the lack of synchronization/versioning around the *swap itself* (as opposed to the conntrack table, which is versioned) means a packet being evaluated during the transition window can be evaluated against inconsistent/half-updated state.

### Finding Description
`Interface` declares the firewall as a plain pointer field: [1](#0-0) 

`reloadFirewall` builds a brand-new `Firewall` object and then replaces the field with a direct assignment, only holding the *old* firewall's conntrack lock (which protects the conntrack map, not the `Interface.firewall` pointer itself): [2](#0-1) 

Meanwhile, the hot path that enforces firewall policy on every packet reads `f.firewall` directly with no lock and no atomic load, from potentially many concurrent tun/udp routines: [3](#0-2) 

This mirrors the reported bug class precisely: the "allowance" (firewall ruleset) is being changed by a trusted actor (operator SIGHUP / cert rotation callback in `reloadFirewall`), while an in-flight "spender" (a remote packet arriving mid-swap) is concurrently being evaluated by `Drop()`/`inConns()` against `f.firewall`. Because the pointer swap is not synchronized against readers (no `atomic.Pointer[Firewall]`, no RWMutex), the Go memory model does not guarantee that concurrent goroutines observe the swap atomically or in a consistent order relative to other fields the reader dereferences off the new `*Firewall` (e.g. `rulesVersion`, `InRules`/`OutRules`, `routableNetworks`), unlike the CA pool/cert state, which correctly use `atomic.Value` for exactly this reason: [4](#0-3) [5](#0-4) 

The conntrack "rulesVersion" mechanism was specifically added to safely re-validate old connections against a new ruleset (CHANGELOG #233), which shows the authors were aware that firewall reloads must not let packets from a moment "before" a change be honored under stale permissions — but that protection lives entirely on the `FirewallConntrack` mutex and does nothing to protect the un-guarded top-level pointer swap that readers dereference first: [6](#0-5) 

### Impact Explanation
If exploitable in practice (Go's race detector would flag `f.firewall = fw` as a data race against concurrent `f.firewall.Drop(...)` calls), the impact is a firewall-enforcement bypass or crash: a packet from a remote, non-CA-authenticated peer could be evaluated against a torn/inconsistent `*Firewall` value during the reload window, or against the pre-reload rule set even though the operator intended immediate tightening (e.g. revoking a previously allowed group/CIDR), analogous to Bob transferring both N and M tokens instead of the intended single value. In the worst case this is a remote crash (nil/garbage dereference of a partially observed struct) or a transient authorization bypass of the newly narrowed firewall policy.

### Likelihood Explanation
Firewall reloads happen whenever an operator changes the `firewall` config section or rotates a certificate whose `UnsafeNetworks` differ (`certUnsafeChanged` in `reloadFirewall`), which is a routine operational event (SIGHUP, cert rotation). Any concurrently-running tun/UDP packet-processing routine is a potential racer, and with `tun.routines`/`listen.routines` > 1 the number of concurrent readers increases, increasing the race window's practical hit rate. No attacker action is required to trigger the reload itself (mirroring Alice's own approve() call in the report); the attacker only needs to be sending traffic during a normal operational reload.

### Recommendation
Protect the `Interface.firewall` field with the same pattern already used for `PKI.cs`/`PKI.caPool`: store it as `atomic.Pointer[Firewall]` and swap it with `Store`, with all readers using `Load()`, so that the swap is atomic and readers never observe a torn/partial update. Additionally, consider fencing packet processing during the swap (e.g., briefly draining or serializing via existing conntrack lock scope extended to cover the pointer swap) so no packet is ever evaluated against a `Firewall` object whose `InRules`/`OutRules`/`rulesVersion` are not mutually consistent.

### Proof of Concept
1. Run nebula with `tun.routines`/`listen.routines` > 1 so multiple goroutines call `f.firewall.Drop(...)` concurrently, per: [3](#0-2) 
2. Continuously send traffic that exercises `Drop()` from a remote (non-CA) peer address while, on the operator side, repeatedly trigger `reloadFirewall` (e.g., via config SIGHUP toggling `firewall.inbound`/`outbound` rules), hitting the unsynchronized swap path: [2](#0-1) 
3. Run with `go test -race`/`-race` build flags under load to observe the data race on `f.firewall`, confirming that reader goroutines are not synchronized against the writer — the concrete, provable defect analogous to the reported approve/transferFrom race window.

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

**File:** interface.go (L402-428)
```go
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
```

**File:** firewall.go (L425-479)
```go
func (f *Firewall) Drop(fp firewall.Packet, incoming bool, h *HostInfo, caPool *cert.CAPool, localCache firewall.ConntrackCache) error {
	// Make sure remote address matches nebula certificate, and determine how to treat it
	if h.networks == nil {
		// Simple case: Certificate has one address and no unsafe networks
		if h.vpnAddrs[0] != fp.RemoteAddr {
			f.metrics(incoming).droppedRemoteAddr.Inc(1)
			return ErrInvalidRemoteIP
		}
	} else {
		nwType, ok := h.networks.Lookup(fp.RemoteAddr)
		if !ok {
			f.metrics(incoming).droppedRemoteAddr.Inc(1)
			return ErrInvalidRemoteIP
		}
		switch nwType {
		case NetworkTypeVPN:
			break // nothing special
		case NetworkTypeVPNPeer:
			f.metrics(incoming).droppedRemoteAddr.Inc(1)
			return ErrPeerRejected // reject for now, one day this may have different FW rules
		case NetworkTypeUnsafe:
			break // nothing special, one day this may have different FW rules
		default:
			f.metrics(incoming).droppedRemoteAddr.Inc(1)
			return ErrUnknownNetworkType //should never happen
		}
	}

	// Make sure we are supposed to be handling this local ip address
	if !f.routableNetworks.Contains(fp.LocalAddr) {
		f.metrics(incoming).droppedLocalAddr.Inc(1)
		return ErrInvalidLocalIP
	}

	// Check if we spoke to this tuple, if we did then allow this packet
	if f.inConns(fp, h, caPool, localCache) {
		return nil
	}

	table := f.OutRules
	if incoming {
		table = f.InRules
	}

	// We now know which firewall table to check against
	if !table.match(fp, incoming, h.ConnectionState.peerCert, caPool) {
		f.metrics(incoming).droppedNoRule.Inc(1)
		return ErrNoMatchingRule
	}

	// We always want to conntrack since it is a faster operation
	f.addConn(fp, incoming)

	return nil
}
```

**File:** pki.go (L69-75)
```go
func (p *PKI) GetCAPool() *cert.CAPool {
	return p.caPool.Load()
}

func (p *PKI) getCertState() *CertState {
	return p.cs.Load()
}
```

**File:** pki.go (L196-205)
```go
func (p *PKI) reloadCAPool(c *config.C) *util.ContextualError {
	caPool, err := loadCAPoolFromConfig(p.l, c)
	if err != nil {
		return util.NewContextualError("Failed to load ca from config", nil, err)
	}

	p.caPool.Store(caPool)
	p.l.Debug("Trusted CA fingerprints", "fingerprints", caPool.GetFingerprints())
	return nil
}
```

**File:** CHANGELOG.md (L727-731)
```markdown

- Previously, we would drop the conntrack table whenever firewall rules were
  changed during a SIGHUP. Now, we will maintain the table and just validate
  that an entry still matches with the new rule set. (#233)

```
