### Title
Unsynchronized read of `hostinfo.remotes.relays` in `relayManager.StartRelays` races with lock-protected writers - ([File: relay_manager.go])

### Summary
`relayManager.StartRelays` reads `hostinfo.remotes.relays` directly (twice, without taking the `RemoteList` lock), while every other accessor of that same field takes `RemoteList.Lock`/`RLock` per the type's own contract ("Every interaction with internals requires a lock!"). An attacker who is not a lighthouse or CA-trusted host can still cause writes to that slice via a `HostUpdateNotification` handled by `handleHostUpdateNotification`, which calls `unlockedSetRelay`/`unlockedCollect` under `RemoteList.Lock`. Concurrently, `StartRelays` runs on the handshake path and reads the field with no lock at all, producing a genuine, `-race`-detectable data race on a slice header shared between goroutines.

### Finding Description
`relay_manager.go` accesses the relay list like this: [1](#0-0) 

Both `len(hostinfo.remotes.relays)` and `relays := hostinfo.remotes.relays` touch the field directly with no `RLock()`/`Unlock()` around it, even though `RemoteList` is explicitly documented as requiring a lock for any internal access: [2](#0-1) 

The writer side, reached from an unauthenticated `HostUpdateNotification` lighthouse packet on a node acting as a lighthouse, takes the lock correctly before mutating `relays` via `unlockedSetRelay`/`unlockedCollect`: [3](#0-2) [4](#0-3) [5](#0-4) 

Because `StartRelays` bypasses the lock entirely, the read of the slice header (pointer/len/cap) can race with a concurrent write to the same header performed elsewhere in the codebase (e.g. `unlockedCollect`/`unlockedSort` reslicing `r.relays` under `Lock()`). This is a genuine unsynchronized memory access on a field that the type's own contract says must always be locked, and it is trivially caught by Go's race detector. It is not, however, the "map" access described in the question — `relays` is a `[]netip.Addr` slice field inside `RemoteList`, not a Go map; the hostmap's actual maps (`Hosts`, `Indexes`, `Relays`, `RemoteIndexes`) and `RelayState`'s maps are all correctly guarded by `sync.RWMutex` in every code path reachable from `StartRelays`, `AddRelay`, `EstablishRelay`, and `HandleControlMsg` (all locking via `hm.Lock()/RLock()` or `RelayState.Lock()/RLock()`).

### Impact Explanation
This is a real, unguarded concurrent access to shared mutable state reachable from unauthenticated `HostUpdateNotification` traffic aimed at a lighthouse node, combined with concurrent handshake activity that calls `StartRelays`. Under Go's memory model this is undefined behavior; in the worst case a torn read of the slice header (inconsistent pointer vs. length) could yield an out-of-bounds read when the returned slice is later iterated, which is capable of crashing the process (a scoped "remote node crash from unauthenticated traffic patterns" per the target impact category). It is not, however, a "fatal concurrent map access" as literally described, since no Go map is unsynchronized here — the vulnerable structure is a slice field, and this changes the precise mechanism (a slice-header race) though the practical effect (potential crash) is the same class of issue.

### Likelihood Explanation
Reaching the vulnerable write path requires the target node to be configured as a lighthouse (`amLighthouse: true`) — `handleHostUpdateNotification` returns immediately otherwise. Given that precondition, the race is easy to trigger repeatably: an attacker can send `HostUpdateNotification` packets (no certificate validation of relay contents; only the announcing vpn address is checked against the packet's from-address) at a high rate while any handshake to a peer that has that host's address in its relay list is being retried, since `StartRelays` is invoked on every outbound-handshake retry tick.

### Recommendation
Take `hostinfo.remotes.RLock()`/`RUnlock()` (or use an existing accessor such as a new `CopyRelays()`/`GetRelays()` method on `RemoteList` that locks internally and returns a copy) before reading `hostinfo.remotes.relays` in `StartRelays`, instead of touching the field directly. Audit for any other direct (non-`unlockedXxx`/locked-accessor) reads of `RemoteList` fields outside of `remote_list.go`.

### Proof of Concept
Add a `-race` stress test in `relay_manager_test.go`:
1. Build a `HostInfo` with a `remotes *RemoteList` (from `NewRemoteList`) and a `relayState`.
2. Spawn goroutine A calling `relayManager.StartRelays` in a tight loop for that hostinfo/vpnIp.
3. Spawn goroutine B calling `remoteList.unlockedSetRelay`/or, more realistically, drive it through `LightHouseHandler.handleHostUpdateNotification` in a loop with varying `RelayVpnAddrs`, targeting the same `RemoteList` object backing `hostinfo.remotes`.
4. Run `go test -race -run TestStartRelays_ConcurrentRemoteListRace` and assert the race detector reports a race on `relay_manager.go:63`/`68` vs. `remote_list.go`'s `unlockedSetRelay`/`unlockedCollect`, with no test failure expected in the non-race build (confirming the bug is a synchronization gap, not a functional bug).

### Citations

**File:** relay_manager.go (L61-74)
```go
func (rm *relayManager) StartRelays(f *Interface, vpnIp netip.Addr, hh *HandshakeHostInfo, stage0 []byte) {
	hostinfo := hh.hostinfo
	if !rm.GetUseRelays() || len(hostinfo.remotes.relays) == 0 {
		hh.lastRelays = nil
		return
	}

	relays := hostinfo.remotes.relays
	listLevel := slog.LevelDebug
	prior := hh.lastRelays
	if !slices.Equal(relays, prior) {
		listLevel = slog.LevelInfo
		hh.lastRelays = slices.Clone(relays)
	}
```

**File:** remote_list.go (L190-221)
```go
// RemoteList is a unifying concept for lighthouse servers and clients as well as hostinfos.
// It serves as a local cache of query replies, host update notifications, and locally learned addresses
type RemoteList struct {
	// Every interaction with internals requires a lock!
	sync.RWMutex

	// The full list of vpn addresses assigned to this host
	vpnAddrs []netip.Addr

	// A deduplicated set of underlay addresses. Any accessor should lock beforehand.
	addrs []netip.AddrPort

	// A set of relay addresses. VpnIp addresses that the remote identified as relays.
	relays []netip.Addr

	// These are maps to store v4 and v6 addresses per lighthouse
	// Map key is the vpnIp of the person that told us about this the cached entries underneath.
	// For learned addresses, this is the vpnIp that sent the packet
	cache map[netip.Addr]*cache

	hr *hostnamesResults

	// shouldAdd is a nillable function that decides if x should be added to addrs.
	shouldAdd func(vpnAddrs []netip.Addr, x netip.Addr) bool

	// This is a list of remotes that we have tried to handshake with and have returned from the wrong vpn ip.
	// They should not be tried again during a handshake
	badRemotes []netip.AddrPort

	// A flag that the cache may have changed and addrs needs to be rebuilt
	shouldRebuild bool
}
```

**File:** remote_list.go (L471-480)
```go
func (r *RemoteList) unlockedSetRelay(ownerVpnIp netip.Addr, to []netip.Addr) {
	r.shouldRebuild = true
	c := r.unlockedGetOrMakeRelay(ownerVpnIp)

	// Reset the slice
	c.relay = c.relay[:0]

	// We can't take their array but we can take their pointers
	c.relay = append(c.relay, to[:minInt(len(to), MaxRemotes)]...)
}
```

**File:** remote_list.go (L642-651)
```go
func (r *RemoteList) unlockedSort(preferredRanges []netip.Prefix) {
	// Use a map to deduplicate any relay addresses
	dedupedRelays := map[netip.Addr]struct{}{}
	for _, relay := range r.relays {
		dedupedRelays[relay] = struct{}{}
	}
	r.relays = r.relays[:0]
	for relay := range dedupedRelays {
		r.relays = append(r.relays, relay)
	}
```

**File:** lighthouse.go (L1330-1375)
```go
func (lhh *LightHouseHandler) handleHostUpdateNotification(n *NebulaMeta, fromVpnAddrs []netip.Addr, w EncWriter) {
	if !lhh.lh.amLighthouse {
		if lhh.l.Enabled(context.Background(), slog.LevelDebug) {
			lhh.l.Debug("I am not a lighthouse, do not take host updates", "from", fromVpnAddrs)
		}
		return
	}

	// not using GetVpnAddrAndVersion because we don't want to error on a blank detailsVpnAddr
	var detailsVpnAddr netip.Addr
	var useVersion cert.Version
	if n.Details.OldVpnAddr != 0 { //v1 always sets this field
		b := [4]byte{}
		binary.BigEndian.PutUint32(b[:], n.Details.OldVpnAddr)
		detailsVpnAddr = netip.AddrFrom4(b)
		useVersion = cert.Version1
	} else if n.Details.VpnAddr != nil { //this field is "optional" in v2, but if it's set, we should enforce it
		detailsVpnAddr = protoAddrToNetAddr(n.Details.VpnAddr)
		useVersion = cert.Version2
	} else {
		detailsVpnAddr = netip.Addr{}
		useVersion = cert.Version2
	}

	//Simple check that the host sent this not someone else, if detailsVpnAddr is filled
	if detailsVpnAddr.IsValid() && !slices.Contains(fromVpnAddrs, detailsVpnAddr) {
		if lhh.l.Enabled(context.Background(), slog.LevelDebug) {
			lhh.l.Debug("Host sent invalid update",
				"vpnAddrs", fromVpnAddrs,
				"answer", detailsVpnAddr,
			)
		}
		return
	}

	relays := n.Details.GetRelays()

	lhh.lh.Lock()
	am := lhh.lh.unlockedGetRemoteList(fromVpnAddrs)
	am.Lock()
	lhh.lh.Unlock()

	am.unlockedSetV4(fromVpnAddrs[0], fromVpnAddrs[0], n.Details.V4AddrPorts, lhh.lh.unlockedShouldAddV4)
	am.unlockedSetV6(fromVpnAddrs[0], fromVpnAddrs[0], n.Details.V6AddrPorts, lhh.lh.unlockedShouldAddV6)
	am.unlockedSetRelay(fromVpnAddrs[0], relays)
	am.Unlock()
```
