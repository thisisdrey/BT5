This confirms the mechanism completely: `connectionManager.makeTrafficDecision` will periodically call `cm.punchy.SendPunch(hostinfo)` and dispatch `sendTestPacket` (`SendMessageToHostInfo(header.Test, ...)`) purely based on hostmap state and traffic timers, without any renewed verification that the stored `hostinfo.remote` was ever reachable. ` [1](#0-0) [2](#0-1) `

### Title
Unverified UDP source address trusted as peer remote during handshake completion, enabling reflection/amplification via periodic punch/test traffic - (File: handshake_manager.go)

### Summary
`HandshakeManager.beginHandshake` completes a responder-side handshake and calls `hostinfo.SetRemote(via.UdpAddr)` using the raw UDP source address of the first handshake packet, with no verification that the sender actually controls that address. `validatePeerCert` only checks `RemoteAllowList.AllowAll`, which defaults to permissive (`al == nil` returns `true`) when no `lighthouse.remote_allow_list` is configured, so an attacker with a valid CA-signed certificate for their own identity can spoof the UDP source and seed a poisoned remote for their own hostinfo entry on the victim responder.

### Finding Description
The exploit path is:
1. `outside.go:readOutsidePackets` routes handshake packets to `f.handshakeManager.HandleIncoming(via, packet, h)` with `via.UdpAddr` taken directly from the received UDP packet's source address. ` [3](#0-2) `
2. `HandshakeManager.HandleIncoming` checks `AllowUnknownVpnAddr(via.UdpAddr.Addr())` for stage-1 packets, then dispatches to `beginHandshake`. ` [4](#0-3) `
3. `beginHandshake` processes the single-message IX handshake; since the responder derives keys and produces `result != nil` after just one inbound message, the tunnel is considered "complete" on the responder side without any round-trip confirmation from the claimed source. ` [5](#0-4) `
4. `validatePeerCert` gates on `f.lightHouse.GetRemoteAllowList().AllowAll(vpnAddrs, via.UdpAddr.Addr())`, but `RemoteAllowList.AllowAll`/`AllowList.Allow` return `true` when no allow list is configured (`al == nil`), which is the default. ` [6](#0-5) [7](#0-6) [8](#0-7) `
5. Immediately afterward, `hostinfo.SetRemote(via.UdpAddr)` stores the (potentially spoofed) source address as the active remote for the newly created `HostInfo`, with no additional check or confirmation step. ` [9](#0-8) [10](#0-9) `

Because UDP is connectionless, an attacker can trivially set the source IP/port of their outbound handshake packet to an arbitrary victim address `X` that they do not control. The responder has no mechanism to confirm return-routability before trusting `via.UdpAddr` as the peer's remote. Subsequently, `connectionManager.makeTrafficDecision` autonomously drives outbound UDP traffic toward whatever `hostinfo.remote` is stored — sending NAT keepalive "punch" packets (`cm.punchy.SendPunch(hostinfo)` / `SendPunchToAll`) and `header.Test` probe packets (`sendTestPacket` → `SendMessageToHostInfo`) on a periodic timer, entirely independent of whether the attacker ever interacts again. ` [1](#0-0) [2](#0-1) ` This turns the responder into a source of unsolicited, recurring UDP traffic directed at an arbitrary third-party address `X`, triggered by a single spoofed packet from the attacker (who never needs to receive the stage-2 response to have poisoned the state).

### Impact Explanation
Scoped impact matches "remote state poisoning" leading to a reflection/DoS primitive against an unrelated third party: a single spoofed stage-1 handshake packet causes the responder to create a hostinfo entry for the attacker's real (CA-valid) identity but with `remote = X`, and the connection manager will then repeatedly send UDP punch/keepalive and Test-request traffic toward `X` for as long as the responder's traffic-check/pending-deletion timers keep the entry alive (bounded by `pendingDeletionInterval`/inactivity teardown, but renewable by the attacker resending spoofed stage-1 packets). This is a low-bandwidth reflection (not large amplification, since handshake/punch/Test payloads are small), but it does allow an unprivileged attacker to make a legitimate Nebula node send unsolicited traffic to a chosen victim IP indefinitely, and to poison the attacker's own tunnel's remote address entirely via spoofing rather than genuine reachability.

### Likelihood Explanation
Preconditions are met by the threat model: attacker only needs a valid CA-signed certificate for their own identity (not the victim's) and the ability to spoof a UDP source address (feasible unless the attacker's own network enforces BCP38 egress filtering — a network-operator control, not a Nebula control). The default `lighthouse.remote_allow_list` config (unset) is permissive, so `AllowAll` does not block this. This is repeatable: each spoofed stage-1 packet can (re)poison the remote, and the responder-side connection manager will resume periodic punch/Test traffic to the spoofed address as long as the pending hostinfo is kept alive.

### Recommendation
Do not fully trust `via.UdpAddr` as an authenticated remote endpoint on first contact. Options: (1) require a round-trip confirmation (e.g., wait for genuine inbound data/Test-response traffic from the claimed address before promoting it to the active `remote` used for autonomous outbound punch/Test traffic, similar to the existing `SetRemoteIfPreferred`/roam-suppression logic already used for later address changes); (2) rate-limit or cap outbound punch/Test traffic to newly-learned, unconfirmed remotes; (3) apply the `AllowAll`/`remote_allow_list` check (or an equivalent reachability gate) consistently before the very first `SetRemote` call in `beginHandshake`, and document that operators relying on the permissive default are exposed to this reflection primitive.

### Proof of Concept
Integration test plan (e2e-style, using existing test harness patterns from `handshake_manager_test.go`/`e2e/handshakes_test.go`):
1. Stand up a responder node `V` with no `lighthouse.remote_allow_list` configured (default permissive).
2. Craft and send a valid stage-1 handshake packet from an attacker identity (CA-signed cert for attacker's own vpn addr) to `V`, but set the UDP source address of the packet to `X` (a third, uninvolved address/port that the attacker does not control) — e.g. by directly invoking `hm.HandleIncoming(via, packet, h)` with `via.UdpAddr = X` while the packet's cryptographic content is legitimately produced by an attacker-controlled `handshake.Machine`.
3. Assert that `hm.beginHandshake` completes: a new `HostInfo` is created and `hostinfo.GetRemote() == X`, i.e. `validatePeerCert`'s `AllowAll` check passed and `SetRemote(via.UdpAddr)` stored `X` without any confirmation that `X` sent or received anything.
4. Drive `connectionManager.makeTrafficDecision` / `doTrafficCheck` forward past `checkInterval` and assert that `cm.punchy.SendPunch` / `SendMessageToHostInfo(header.Test, ...)` calls are made with `hostinfo.remote == X`, confirming outbound UDP traffic is directed at `X` without `X` ever having participated in the handshake.

### Citations

**File:** connection_manager.go (L356-363)
```go
		cm.trafficTimer.Add(hostinfo.localIndexId, cm.checkInterval)

		if !outTraffic {
			// Send a punch packet to keep the NAT state alive
			cm.punchy.SendPunch(hostinfo)
		}

		return decision, hostinfo, primary
```

**File:** connection_manager.go (L389-409)
```go
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
```

**File:** outside.go (L76-79)
```go
	switch h.Type {
	case header.Handshake:
		f.handshakeManager.HandleIncoming(via, packet, h)
		return
```

**File:** handshake_manager.go (L164-184)
```go
	// First remote allow list check before we know the vpnIp
	if !via.IsRelayed {
		if !hm.lightHouse.GetRemoteAllowList().AllowUnknownVpnAddr(via.UdpAddr.Addr()) {
			hm.l.Debug("lighthouse.remote_allow_list denied incoming handshake", "from", via)
			return
		}
	}

	// First message of a new handshake. The wire format requires RemoteIndex
	// to be zero here (the initiator has no responder index to fill in yet),
	// and generateIndex never allocates 0, so any non-zero RemoteIndex on a
	// stage-1 packet is malformed or someone probing for an index collision.
	// Drop without paying the cost of running noise on a pending Machine.
	if h.MessageCounter == 1 {
		if h.RemoteIndex != 0 {
			hm.l.Debug("dropping stage-1 handshake with non-zero RemoteIndex",
				"from", via, "remoteIndex", h.RemoteIndex)
			return
		}
		hm.beginHandshake(via, packet, h)
		return
```

**File:** handshake_manager.go (L722-738)
```go
	response, result, err := machine.ProcessPacket(nil, packet)
	if err != nil {
		f.l.Error("Failed to process handshake packet", "from", via, "error", err)
		return
	}

	if result == nil {
		// Multi-message pattern: the responder Machine would need to be
		// registered in hm.indexes so a future inbound packet finds it via
		// continueHandshake. The current manager doesn't do that yet, so
		// fail loudly rather than silently dropping the in-flight handshake.
		// TODO: support multi-message responder flows (XX, pqIX, etc.).
		// See also the IX-shaped cipher key assignment in handshake.Machine.
		f.l.Error("multi-message handshake responder is not supported",
			"from", via, "error", handshake.ErrMultiMessageUnsupported)
		return
	}
```

**File:** handshake_manager.go (L791-794)
```go
	hostinfo.remotes = f.lightHouse.QueryCache(vpnAddrs)
	if !via.IsRelayed {
		hostinfo.SetRemote(via.UdpAddr)
	}
```

**File:** handshake_manager.go (L1030-1036)
```go
	if !via.IsRelayed {
		if !f.lightHouse.GetRemoteAllowList().AllowAll(vpnAddrs, via.UdpAddr.Addr()) {
			f.l.Debug("lighthouse.remote_allow_list denied incoming handshake",
				"vpnAddrs", vpnAddrs, "from", via)
			return nil, false, false
		}
	}
```

**File:** allow_list.go (L239-246)
```go
func (al *AllowList) Allow(addr netip.Addr) bool {
	if al == nil {
		return true
	}

	result, _ := al.cidrTree.Lookup(addr)
	return result
}
```

**File:** allow_list.go (L284-296)
```go
func (al *RemoteAllowList) AllowAll(vpnAddrs []netip.Addr, udpAddr netip.Addr) bool {
	if !al.AllowList.Allow(udpAddr) {
		return false
	}

	for _, vpnAddr := range vpnAddrs {
		if !al.getInsideAllowList(vpnAddr).Allow(udpAddr) {
			return false
		}
	}

	return true
}
```

**File:** hostmap.go (L777-783)
```go
func (i *HostInfo) SetRemote(remote netip.AddrPort) {
	// We copy here because we likely got this remote from a source that reuses the object
	if i.GetRemote() != remote {
		i.remote.Store(&remote)
		i.remotes.LearnRemote(i.vpnAddrs[0], remote)
	}
}
```
