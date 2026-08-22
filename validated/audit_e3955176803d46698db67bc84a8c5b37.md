### Title
Unauthenticated handshake replay triggers `SetRemoteIfPreferred` remote-address roaming, enabling remote state poisoning of an established tunnel — (File: `handshake_manager.go`, `hostmap.go`)

### Summary
Nebula's handshake-completion dedup path (`ErrAlreadySeen`) treats a byte-for-byte replay of a previously seen handshake packet as evidence that the *sender's current UDP source address* is a legitimate roam target and calls `HostInfo.SetRemoteIfPreferred`, which can permanently switch a live tunnel's remote endpoint based purely on the transport-layer source address of the replayed packet — an address that is never itself authenticated.

### Finding Description
When a stage-0 handshake packet arrives that duplicates bytes already stored on an existing `HostInfo` (a legitimate retransmit scenario), `CheckAndComplete` returns `ErrAlreadySeen` after only comparing raw packet bytes, with no check that the new packet actually originated from the address it claims to be from: [1](#0-0) 

`handleCheckAndCompleteError` then treats this as a roaming signal and calls `SetRemoteIfPreferred` with `via`, the attacker-controlled sender address of the *replayed* packet, immediately resending the original cached handshake response to that address: [2](#0-1) 

`SetRemoteIfPreferred` then unconditionally rewrites the tunnel's active remote to the new (unauthenticated) source address as long as it falls inside the configured `preferred_ranges`: [3](#0-2) 

The only gate on this state change is membership in `preferred_ranges` — a purely address-based allow-list (commonly configured to include private/LAN ranges like `10.0.0.0/8` or `192.168.0.0/16` to prefer local paths over public IPs). Nothing about the noise handshake, cert signature, or AEAD counters is re-verified for the *replayed* packet itself; the trust is entirely inherited from the original (already-completed) handshake, while the roam decision is driven by the untrusted UDP source of the replay. An attacker who can observe/capture one legitimate stage-0 handshake packet (e.g., by being on the same L2/L3 segment that falls in `preferred_ranges`, which is exactly the population these ranges are meant to include) can replay it from an address they control and redirect the victim's established tunnel traffic to that address.

This mirrors the reported bug-class: a third party with no proper "credential" over the victim's connection (no valid CA-signed cert of their own, just possession of a captured packet) can flip persistent victim state (the tunnel's remote endpoint) through a narrow, address-triggered side-channel (`SetRemoteIfPreferred`) rather than through the primary authenticated path, exactly as `rebalanceUp` let an uncredentialed flashloan actor flip a victim's locked-in stable rate through a side-effect of a legitimate rebalancing mechanism.

### Impact Explanation
Successful exploitation redirects a victim's live tunnel traffic (all outgoing encrypted data-plane packets to that peer) to an attacker-chosen address that lies within `preferred_ranges`. This is remote state poisoning of the hostmap/routing state: it can be used to black-hole traffic (denial of service against a specific peer) or, combined with the attacker's position on the preferred network, to intercept/analyze future traffic patterns sent to the wrong endpoint. Because `preferred_ranges` are typically broad private-network ranges shared by many hosts on a LAN, any host on that LAN segment can potentially replay a sniffed packet to hijack another peer's roam decision.

### Likelihood Explanation
Medium: it requires (1) `preferred_ranges` to be configured (a common but optional feature for LAN performance), (2) the attacker to capture one legitimate handshake stage-0 packet between two peers (feasible for any on-path/same-segment observer), and (3) the attacker to be able to send from an address within the configured preferred range (often trivial on a shared LAN or via source spoofing where ingress filtering is weak).

### Recommendation
Do not let the *source address of a replayed/duplicate handshake packet* participate in roam decisions. `SetRemoteIfPreferred` should only be invoked from packets whose cryptographic authenticity for *that specific transmission* has been freshly verified (e.g., post-handshake authenticated messages with fresh AEAD counters), not from a byte-for-byte replay match. At minimum, gate the `ErrAlreadySeen` roam side-effect behind additional confirmation such as requiring a subsequent authenticated data-plane message from the new address before committing the roam.

### Proof of Concept
1. Configure two nebula nodes with `preferred_ranges` including a shared LAN prefix (e.g., `10.0.0.0/8`), and complete a handshake between them (as in `handshake_manager.go`'s `beginHandshake`, which stores `hostinfo.HandshakePacket[handshakePacketStage0]`).
2. As a third host on that LAN, capture the initiator's stage-0 handshake packet in flight.
3. Replay the exact captured bytes to the responder from the attacker's own address (which is within `preferred_ranges`).
4. Observe `CheckAndComplete` return `ErrAlreadySeen` (`handshake_manager.go:441-443`) and `handleCheckAndCompleteError`'s `ErrAlreadySeen` branch call `existing.SetRemoteIfPreferred(f.hostMap, via)` (`handshake_manager.go:1105`), which updates `HostInfo.remote` to the attacker's address (`hostmap.go:812-820`) — subsequent tunnel traffic from the responder is now sent to the attacker instead of the original initiator.

### Citations

**File:** handshake_manager.go (L436-444)
```go
	// Check if we already have a tunnel with this vpn ip
	existingHostInfo, found := hm.mainHostMap.Hosts[hostinfo.vpnAddrs[0]]
	if found && existingHostInfo != nil {
		// Is it just a delayed handshake packet? Check every hostinfo we hold for this address.
		for _, testHostInfo := range hm.mainHostMap.unlockedGetHostList(hostinfo.vpnAddrs[0]) {
			if bytes.Equal(hostinfo.HandshakePacket[handshakePacket], testHostInfo.HandshakePacket[handshakePacket]) {
				return testHostInfo, ErrAlreadySeen
			}
		}
```

**File:** handshake_manager.go (L1103-1113)
```go
	switch err {
	case ErrAlreadySeen:
		if existing.SetRemoteIfPreferred(f.hostMap, via) {
			f.SendMessageToVpnAddr(header.Test, header.TestRequest, hostinfo.vpnAddrs[0], []byte(""), make([]byte, 12, 12), make([]byte, mtu))
		}
		// Resend the original response. The peer is committed to that response's
		// ephemeral keys; a freshly-built one would have different keys and break
		// the tunnel even though both sides "completed" the handshake.
		if msg := existing.HandshakePacket[handshakePacketStage2]; msg != nil {
			hm.sendHandshakeResponse(via, msg, existing, true)
		}
```

**File:** hostmap.go (L785-823)
```go
// SetRemoteIfPreferred returns true if the remote was changed. The lastRoam
// time on the HostInfo will also be updated.
func (i *HostInfo) SetRemoteIfPreferred(hm *HostMap, via ViaSender) bool {
	if via.IsRelayed {
		return false
	}

	currentRemote := i.GetRemote()
	if !currentRemote.IsValid() {
		i.SetRemote(via.UdpAddr)
		return true
	}

	// NOTE: We do this loop here instead of calling `isPreferred` in
	// remote_list.go so that we only have to loop over preferredRanges once.
	newIsPreferred := false
	for _, l := range hm.GetPreferredRanges() {
		// return early if we are already on a preferred remote
		if l.Contains(currentRemote.Addr()) {
			return false
		}

		if l.Contains(via.UdpAddr.Addr()) {
			newIsPreferred = true
		}
	}

	if newIsPreferred {
		// Consider this a roaming event
		i.lastRoam = time.Now()
		i.lastRoamRemote = currentRemote

		i.SetRemote(via.UdpAddr)

		return true
	}

	return false
}
```
