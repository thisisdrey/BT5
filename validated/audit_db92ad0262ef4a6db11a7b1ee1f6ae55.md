### Title
Relay state is lost on tunnel teardown because `deleteTunnel`/`closeTunnel` never migrate relay-for state before deleting the hostinfo - (File: connection_manager.go)

### Summary
This is the same bug class as the reported Alchemix issue: state tied to an object (ALCX rewards tied to a veALCX token) is destroyed along with the object instead of being transferred/claimed first. In `nebula--004` the analogous state is the relay routing state (`relayState`) a `HostInfo` "burns" (is deleted) while still holding, without first migrating it to the surviving/primary sibling `HostInfo` for the same peer.

### Finding Description
`connectionManager.doTrafficCheck` dispatches on a `trafficDecision` computed in `makeTrafficDecision`: [1](#0-0) 

Only the `migrateRelays` branch calls `cm.migrateRelayUsed(hostinfo, primary)`, which copies the non-primary hostinfo's relay-for entries (`RelayState.CopyAllRelayFor`) onto the primary hostinfo before the non-primary one can be reaped: [2](#0-1) 

The `deleteTunnel` and `closeTunnel` branches, by contrast, delete the hostinfo directly with no migration step at all: [3](#0-2) 

`makeTrafficDecision` reaches `deleteTunnel`/`closeTunnel` for a hostinfo regardless of whether it is the primary or a non-primary sibling for its vpn address (invalid-certificate check, inactivity-drop check, and dead-tunnel pending-deletion check all run before the primary/non-primary branching, or independently of it): [4](#0-3) [5](#0-4) 

When such a hostinfo is finally removed via `HostMap.DeleteHostInfo` → `unlockedDeleteHostInfo`, its relay-for state ("acting as a relay hop") is simply deleted from `hm.Relays`, and the "rebuild relay" signal (`unlockedDisestablishVpnAddrRelayFor`) is only triggered when the deleted hostinfo was the *last* one for that vpn address (`final == true`): [6](#0-5) 

So whenever a `HostInfo` that is carrying relay-for state is torn down via `deleteTunnel` or `closeTunnel` while a sibling `HostInfo` for the same peer still exists (`final == false`), its relay state is deleted with no migration to the surviving sibling and no rebuild trigger — the relay routes it was maintaining are silently and permanently dropped, exactly mirroring the reported pattern of burning a token without first claiming/transferring the value it held. The existing `migrateRelayUsed` codepath and the changelog entries about relay re-establishment (`#1805`, `#1796`, `#1753`) show the project is aware this class of "burn before claim" bug exists for relay state, but that fix was only wired into the `migrateRelays` decision, not into `deleteTunnel`/`closeTunnel`.

### Impact Explanation
Loss of relay-for state without triggering a rebuild leaves the peer that was relying on this node as a relay hop with a stale/unusable route: sends through the affected relay path silently fail until something else (e.g., dead-tunnel detection) forces a fresh handshake and relay re-establishment. This is a remote state-poisoning / availability impact on the mesh's relay path — traffic that should be relayed is silently dropped for an unbounded period, which is the closest match in-scope to "permanent loss/freezing" for this codebase (no rewards/tokens exist here; relay routing state is the closest analog of "value" that can be silently and permanently lost on teardown).

### Likelihood Explanation
This triggers under ordinary operating conditions with no attacker action needed: any node using relays that has multiple concurrent `HostInfo` entries for the same peer (a routine situation during re-handshakes/roaming) and whose non-primary or even primary hostinfo gets torn down via inactivity timeout, certificate invalidation, or dead-tunnel detection while a sibling survives, will hit this path. No malicious peer, lighthouse, or CA-signed certificate is required — it's a lifecycle-management gap in `connectionManager`.

### Recommendation
In `doTrafficCheck`'s `deleteTunnel` and `closeTunnel` cases, before deleting the hostinfo, look up the current primary hostinfo for the same vpn address and, if it differs from the one being torn down, call `cm.migrateRelayUsed(hostinfo, primary)` (the same helper already used in the `migrateRelays` case) to transfer any relay-for state prior to deletion. Alternatively, make `unlockedDeleteHostInfo` itself migrate relay-for entries to the new primary whenever `final == false`, so no caller can accidentally burn relay state.

### Proof of Concept
Conceptual reproduction (mirrors the reported PoC pattern of "claim before burn" being skipped):
1. Node `A` uses relay `R` to reach peer `B`, and `A` holds two `HostInfo`s for `B` (e.g., after a re-handshake creates a second tunnel before the old one is reaped) — the older, non-primary `HostInfo` carries the established relay-for state (`relayState`) for `R`/`B`.
2. Let the older `HostInfo` go idle/inactive (no traffic) so `makeTrafficDecision` marks it `pendingDeletion` and, on the following tick, returns `deleteTunnel` for it (see `connection_manager.go:366-373`).
3. `doTrafficCheck`'s `deleteTunnel` branch calls `hostMap.DeleteHostInfo(hostinfo)` directly with no call to `migrateRelayUsed` (`connection_manager.go:170-174`), while the primary `HostInfo` (the sibling that survives) never receives the migrated relay entry.
4. `unlockedDeleteHostInfo` deletes the relay index from `hm.Relays` and, because `final == false` (the sibling still owns the address), never calls `unlockedDisestablishVpnAddrRelayFor` to mark the relay for rebuild (`hostmap.go:533-541`).
5. Result: `A`'s relay path through `R` to `B` is permanently gone from `A`'s live (primary) hostinfo; traffic that should go through `R` silently fails until an unrelated event (e.g. dead-tunnel detection) forces a fresh relay handshake — the equivalent of the ALCX rewards becoming "lost and unclaimable" once the token (hostinfo) is burned.

### Citations

**File:** connection_manager.go (L166-194)
```go
func (cm *connectionManager) doTrafficCheck(localIndex uint32, p, nb, out []byte, now time.Time) {
	decision, hostinfo, primary := cm.makeTrafficDecision(localIndex, now)

	switch decision {
	case deleteTunnel:
		if cm.hostMap.DeleteHostInfo(hostinfo) {
			// Only clearing the lighthouse cache if this is the last hostinfo for this vpn ip in the hostmap
			cm.intf.lightHouse.DeleteVpnAddrs(hostinfo.vpnAddrs)
		}

	case closeTunnel:
		cm.intf.sendCloseTunnel(hostinfo)
		cm.intf.closeTunnel(hostinfo)

	case swapPrimary:
		cm.swapPrimary(hostinfo, primary)

	case migrateRelays:
		cm.migrateRelayUsed(hostinfo, primary)

	case tryRehandshake:
		cm.tryRehandshake(hostinfo)

	case sendTestPacket:
		cm.intf.SendMessageToHostInfo(header.Test, header.TestRequest, hostinfo, p, nb, out)
	}

	cm.resetRelayTrafficCheck(hostinfo)
}
```

**File:** connection_manager.go (L207-212)
```go
func (cm *connectionManager) migrateRelayUsed(oldhostinfo, newhostinfo *HostInfo) {
	relayFor := oldhostinfo.relayState.CopyAllRelayFor()

	for _, r := range relayFor {
		existing, ok := newhostinfo.relayState.QueryRelayForByIp(r.PeerAddr)

```

**File:** connection_manager.go (L311-373)
```go
func (cm *connectionManager) makeTrafficDecision(localIndex uint32, now time.Time) (trafficDecision, *HostInfo, *HostInfo) {
	// Read lock the main hostmap to order decisions based on tunnels being the primary tunnel
	cm.hostMap.RLock()
	defer cm.hostMap.RUnlock()

	hostinfo := cm.hostMap.Indexes[localIndex]
	if hostinfo == nil {
		cm.l.Debug("Not found in hostmap", "localIndex", localIndex)
		return doNothing, nil, nil
	}

	if cm.isInvalidCertificate(now, hostinfo) {
		return closeTunnel, hostinfo, nil
	}

	primary := cm.hostMap.Hosts[hostinfo.vpnAddrs[0]]
	mainHostInfo := true
	if primary != nil && primary != hostinfo {
		mainHostInfo = false
	}

	// Check for traffic on this hostinfo
	inTraffic, outTraffic := cm.getAndResetTrafficCheck(hostinfo, now)

	// A hostinfo is determined alive if there is incoming traffic
	if inTraffic {
		decision := doNothing
		if cm.l.Enabled(context.Background(), slog.LevelDebug) {
			hostinfo.logger(cm.l).Debug("Tunnel status",
				"tunnelCheck", m{"state": "alive", "method": "passive"},
			)
		}
		hostinfo.pendingDeletion.Store(false)

		if mainHostInfo {
			decision = tryRehandshake
		} else {
			if cm.shouldSwapPrimary(hostinfo) {
				decision = swapPrimary
			} else {
				// migrate the relays to the primary, if in use.
				decision = migrateRelays
			}
		}

		cm.trafficTimer.Add(hostinfo.localIndexId, cm.checkInterval)

		if !outTraffic {
			// Send a punch packet to keep the NAT state alive
			cm.punchy.SendPunch(hostinfo)
		}

		return decision, hostinfo, primary
	}

	if hostinfo.pendingDeletion.Load() {
		// We have already sent a test packet and nothing was returned, this hostinfo is dead
		hostinfo.logger(cm.l).Info("Tunnel status",
			"tunnelCheck", m{"state": "dead", "method": "active"},
		)

		return deleteTunnel, hostinfo, nil
	}
```

**File:** connection_manager.go (L375-419)
```go
	decision := doNothing
	if hostinfo != nil && hostinfo.ConnectionState != nil && mainHostInfo {
		if !outTraffic {
			inactiveFor, isInactive := cm.isInactive(hostinfo, now)
			if isInactive {
				// Tunnel is inactive, tear it down
				hostinfo.logger(cm.l).Info("Dropping tunnel due to inactivity",
					"inactiveDuration", inactiveFor,
					"primary", mainHostInfo,
				)

				return closeTunnel, hostinfo, primary
			}

			// If we aren't sending or receiving traffic then its an unused tunnel and we don't to test the tunnel.
			// Just maintain NAT state if configured to do so.
			cm.punchy.SendPunch(hostinfo)
			cm.trafficTimer.Add(hostinfo.localIndexId, cm.checkInterval)
			return doNothing, nil, nil
		}

		// We aren't receiving traffic but we are sending it. The outbound
		// traffic itself refreshes the primary remote's NAT state; this
		// fans out to non-primary remotes, but only if target_all_remotes
		// is configured.
		cm.punchy.SendPunchToAll(hostinfo)

		if cm.l.Enabled(context.Background(), slog.LevelDebug) {
			hostinfo.logger(cm.l).Debug("Tunnel status",
				"tunnelCheck", m{"state": "testing", "method": "active"},
			)
		}

		// Send a test packet to trigger an authenticated tunnel test, this should suss out any lingering tunnel issues
		decision = sendTestPacket

	} else {
		if cm.l.Enabled(context.Background(), slog.LevelDebug) {
			hostinfo.logger(cm.l).Debug("Hostinfo sadness")
		}
	}

	hostinfo.pendingDeletion.Store(true)
	cm.trafficTimer.Add(hostinfo.localIndexId, cm.pendingDeletionInterval)
	return decision, hostinfo, nil
```

**File:** hostmap.go (L476-543)
```go
// unlockedDeleteHostInfo removes hostinfo from every one of its address lists and from the index
// maps. It returns true if this was the last hostinfo for all of its addresses (we no longer have
// any tunnel to the peer), which the caller uses to decide whether to clear learned lighthouse
// state and disestablish relays.
func (hm *HostMap) unlockedDeleteHostInfo(hostinfo *HostInfo) bool {
	// Remove this hostinfo from each of its address lists. The lists are independent, so a
	// sibling is never promoted to an address it does not own and no other list is touched.
	final := true
	for _, addr := range hostinfo.vpnAddrs {
		if list, ok := hm.moreHosts[addr]; ok {
			list = removeHostInfo(list, hostinfo)
			hm.unlockedSetHostsForAddr(addr, list)
			if len(list) > 0 {
				final = false
			}
		} else if existing, ok := hm.Hosts[addr]; ok {
			if existing == hostinfo {
				// Common case, the only hostinfo for this address. moreHosts has no entry to clean up.
				delete(hm.Hosts, addr)
			} else {
				// We don't hold this address but another hostinfo does, we still have a tunnel to the peer
				final = false
			}
		}
	}

	// Go maps never shrink their buckets, replace fully drained maps so a node that churned
	// through a large peer count gives the memory back. Same idiom as the index maps below.
	if len(hm.Hosts) == 0 {
		hm.Hosts = map[netip.Addr]*HostInfo{}
	}
	if len(hm.moreHosts) == 0 {
		hm.moreHosts = map[netip.Addr][]*HostInfo{}
	}

	// The remote index uses index ids outside our control so lets make sure we are only removing
	// the remote index pointer here if it points to the hostinfo we are deleting
	hostinfo2, ok := hm.RemoteIndexes[hostinfo.remoteIndexId]
	if ok && hostinfo2 == hostinfo {
		delete(hm.RemoteIndexes, hostinfo.remoteIndexId)
		if len(hm.RemoteIndexes) == 0 {
			hm.RemoteIndexes = map[uint32]*HostInfo{}
		}
	}

	delete(hm.Indexes, hostinfo.localIndexId)
	if len(hm.Indexes) == 0 {
		hm.Indexes = map[uint32]*HostInfo{}
	}

	if hm.l.Enabled(context.Background(), slog.LevelDebug) {
		hm.l.Debug("Hostmap hostInfo deleted",
			"hostMap", m{"mapTotalSize": len(hm.Hosts),
				"vpnAddrs": hostinfo.vpnAddrs, "indexNumber": hostinfo.localIndexId, "remoteIndexNumber": hostinfo.remoteIndexId},
		)
	}

	if final {
		// I have lost connectivity to my peers. My relay tunnel is likely broken. Mark the next
		// hops as 'Requested' so that new relay tunnels are created in the future.
		hm.unlockedDisestablishVpnAddrRelayFor(hostinfo)
	}
	// Clean up any local relay indexes for which I am acting as a relay hop
	for _, localRelayIdx := range hostinfo.relayState.CopyRelayForIdxs() {
		delete(hm.Relays, localRelayIdx)
	}

	return final
```
