### Title
Blocklist for malicious handshake responders is wiped on every handshake refresh, letting a blocked spoofed address be retried - ([File: remote_list.go])

### Summary
`RemoteList.RefreshFromHandshake` unconditionally clears `r.badRemotes` every time a handshake completes, discarding the record of addresses that were previously blocked for responding to a handshake with the wrong host's certificate.

### Finding Description
When a handshake responder turns out to be the wrong host (an address that answered a handshake attempt with a certificate for a different VPN address than intended), Nebula calls `hostinfo.remotes.BlockRemote(via)` before restarting the handshake, specifically to keep that address out of the candidate remote-address pool used for future handshake attempts (`handshake_manager.go:936-943`): [1](#0-0) 

This blocklist (`badRemotes`) is the *only* persistent state tracking that a given `netip.AddrPort` misbehaved during handshake negotiation. However, `RefreshFromHandshake`, which runs immediately after every successfully completed handshake (both as the pending-handshake responder path and the initiator/continuation path), resets it to `nil` unconditionally: [2](#0-1) 

`RefreshFromHandshake` is invoked from both `beginHandshake` and `continueHandshake` on every handshake completion: [3](#0-2) [4](#0-3) 

This mirrors the analog bug class exactly: a security-relevant accumulator (`badRemotes`, meant to "compound" knowledge of bad actors over the life of a `RemoteList`) is unconditionally reset to its zero/insecure baseline by an unrelated, frequently-occurring event (a completed handshake — comparable to the oracle's `fulfill` callback resetting `oraclePrice`/`startTime`), silently discarding the protective state instead of preserving/merging it. `RemoteList` objects are long-lived and shared across repeated handshake cycles (roaming, rekeys, `punchy` retriggers), so this reset happens routinely, not just once.

### Impact Explanation
An attacker that can inject UDP traffic (spoofed source address/port) toward a victim, and cause a `HandshakeWrongResponderPacketStore`-style situation (respond to a handshake attempt with a certificate for the wrong VPN address, which the e2e test `TestHandshakeWrongResponderPacketStore` shows is explicitly guarded against by blocking the offending address), gets that address purged from the block list the very next time the victim completes a legitimate handshake with anyone tracked through that `RemoteList`. This re-admits the previously-blocked address into `remotes.ForEach`/`CopyAddrs` candidate selection used by `handleOutbound` (`handshake_manager.go:270,294`) for subsequent handshake attempts, allowing the same spoofed/impostor address to be retried indefinitely rather than being permanently excluded. This is a remote-state-poisoning class issue: protective state that should compound/persist across the tunnel's lifetime is instead reset to empty by ordinary protocol events, undermining the intended defense-in-depth around handshake target selection and enabling repeated resource-wasting/handshake-hijack-attempt amplification against a peer that has no way to permanently learn "this address is bad."

### Likelihood Explanation
Triggering the block requires only the ability to answer a handshake stage-1 packet with any valid certificate signed by a CA in the mesh's trust pool but for an address other than the one being contacted (no valid cert for the *targeted* identity is needed — this is purely a mismatch, and the finding notes such addresses get blocked precisely because they are untrusted for that vpnAddr). Because `RefreshFromHandshake` fires after *every* completed handshake for that `RemoteList` (which happens frequently due to roaming, punchy retriggers, and periodic re-handshakes), the block is trivially and repeatedly undone without any special timing or race — making this a highly reachable, low-effort condition.

### Recommendation
`RefreshFromHandshake` should not reset `badRemotes`; blocked/bad remotes should persist independently of handshake completion (e.g., merge newly-learned addresses instead of wiping the blocklist, or move blocklist expiration to an explicit, address-scoped TTL rather than a blanket reset on every handshake success).

### Proof of Concept
1. Peer `me` attempts a handshake toward `them`'s VPN address, but an attacker-controlled `evil` UDP endpoint (registered via a poisoned lighthouse entry, as in `TestHandshakeWrongResponderPacketStore`) responds first with a valid cert for a *different* identity.
2. `continueHandshake` detects the mismatch (`correctHostResponded == false`), calls `hm.DeleteHostInfo` and `hm.StartHandshake` with a callback that does `newHH.hostinfo.remotes.BlockRemote(via)` (`handshake_manager.go:936-943`), adding `evil`'s address to `badRemotes`.
3. `me` eventually completes a legitimate handshake with the real `them` (or any other handshake on that same `RemoteList`), which calls `hostinfo.remotes.RefreshFromHandshake(vpnAddrs)` (`handshake_manager.go:804`/`983`), which unconditionally sets `r.badRemotes = nil` (`remote_list.go:413`).
4. `evil`'s address is now eligible again for selection in future `handleOutbound` handshake attempts against the same target, even though it was previously and deliberately blocked for impersonation-style misbehavior. [2](#0-1)  confirms the unconditional reset; [1](#0-0)  confirms the block is added for exactly this misbehavior; [3](#0-2)  and [4](#0-3)  confirm the reset is triggered by routine, frequent handshake completion.

### Citations

**File:** handshake_manager.go (L803-804)
```go
	hm.sendHandshakeResponse(via, response, hostinfo, false)
	hostinfo.remotes.RefreshFromHandshake(vpnAddrs)
```

**File:** handshake_manager.go (L936-943)
```go
		hm.StartHandshake(hostinfo.vpnAddrs[0], func(newHH *HandshakeHostInfo) {
			newHH.hostinfo.remotes = hostinfo.remotes
			newHH.hostinfo.remotes.BlockRemote(via)
			newHH.packetStore = hh.packetStore
			hh.packetStore = []*cachedPacket{}
			hostinfo.vpnAddrs = vpnAddrs
			f.sendCloseTunnel(hostinfo)
		})
```

**File:** handshake_manager.go (L983-984)
```go
	hostinfo.remotes.RefreshFromHandshake(vpnAddrs)
	f.metricHandshakes.Update(duration)
```

**File:** remote_list.go (L410-417)
```go
// RefreshFromHandshake locks and updates the RemoteList to account for data learned upon a completed handshake
func (r *RemoteList) RefreshFromHandshake(vpnAddrs []netip.Addr) {
	r.Lock()
	r.badRemotes = nil
	r.vpnAddrs = make([]netip.Addr, len(vpnAddrs))
	copy(r.vpnAddrs, vpnAddrs)
	r.Unlock()
}
```
