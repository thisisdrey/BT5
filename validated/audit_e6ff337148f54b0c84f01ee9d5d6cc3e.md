### Title
Replayed stage-1 handshake packet allows unauthenticated remote-address takeover of an established tunnel - (File: `handshake_manager.go`, `hostmap.go`)

### Summary
`HandshakeManager.CheckAndComplete` identifies a "duplicate" stage-1 handshake solely by raw byte-equality of the cached packet, and the `ErrAlreadySeen` handler then trusts the UDP source address of that duplicate to update (roam) the already-established tunnel's remote endpoint via `HostInfo.SetRemoteIfPreferred`. Because the stage-1 packet's initiator payload is public wire data that any observer can capture and replay verbatim from a different source address, this "already seen" check functions like a stale, unrevoked authorization: byte-match on old, previously-accepted data is treated as proof of present authorization to change where traffic is sent, without any fresh cryptographic proof tying the packet to the sending IP.

### Finding Description
`CheckAndComplete` in [1](#0-0)  looks up any existing hostinfo for the vpn address and returns `ErrAlreadySeen` purely on `bytes.Equal(hostinfo.HandshakePacket[handshakePacket], testHostInfo.HandshakePacket[handshakePacket])` — a comparison of raw handshake bytes, not a signature or MAC bound to the current sender's address.

`handleCheckAndCompleteError` then handles this case by calling `existing.SetRemoteIfPreferred(f.hostMap, via)` [2](#0-1) , where `via.UdpAddr` is the source address of the *replayed* packet, fully attacker-controlled.

`SetRemoteIfPreferred` in [3](#0-2)  then updates the live tunnel's remote endpoint:
- If the tunnel currently has no valid remote (`!currentRemote.IsValid()`), it unconditionally calls `i.SetRemote(via.UdpAddr)` — no preferred-range check at all.
- Otherwise, if the replayed packet's source address falls in a configured `preferred_ranges` CIDR, it also roams the remote to the attacker's address.

Because a stage-1 handshake message is the initiator's first Noise IX message — sent in the clear over UDP and not bound cryptographically to a specific sender IP — any network observer can capture one legitimate stage-1 packet and later replay the identical bytes from an address they control. This is exactly analogous to the reported NFT bug: an old, byte-identical artifact ("approval") that should have been consumed/invalidated once the handshake context moved on is instead treated as a valid, live authorization to reassign ownership of state (here, the trusted remote UDP endpoint of an established tunnel) to whoever presents it.

### Impact Explanation
An attacker who has observed a single stage-1 packet from a real peer can, at any later time, redirect that peer's outbound-facing tunnel state (`HostInfo.remote`) to an address of the attacker's choosing. Subsequent outbound Nebula traffic intended for the legitimate peer is sent to the attacker's address instead, effectively achieving remote address/state poisoning and traffic interception/redirection without needing to complete a real handshake or hold a CA-signed certificate for either endpoint. This is a "remote state poisoning" impact within the accepted analog classes.

### Likelihood Explanation
Reachable pre-authentication: the attacker only needs to observe one raw stage-1 UDP packet (unencrypted wire content) and replay it verbatim from a spoofed/attacker-controlled UDP source. No valid certificate, private key, or completed handshake is required — only a captured packet and the ability to send UDP packets, which matches "an attacker with no CA-signed certificate." The unconditional branch (`!currentRemote.IsValid()`) requires no `preferred_ranges` configuration at all, making it broadly reachable; the general-preferred-range branch additionally requires that config option to be set, which is common for private overlay networks.

### Recommendation
Do not treat byte-equality of a cached handshake packet as proof that the current sender is authorized to change tunnel remote state. Options:
- Never call `SetRemoteIfPreferred` (or unconditional `SetRemote`) purely off the `ErrAlreadySeen` path; only allow remote-address updates from cryptographically completed/authenticated handshake stages or from packets protected by the session's AEAD keys.
- If a roam-on-duplicate optimization is desired, additionally require a nonce/freshness check (e.g., a replay window or timestamp binding) so a captured-and-replayed stage-1 packet cannot be reused indefinitely by a different source address.
- At minimum, remove the unconditional `SetRemote` call when `currentRemote` is invalid so an attacker cannot unconditionally seed the tunnel's remote before any address is set.

### Proof of Concept
1. Attacker passively captures a legitimate stage-1 handshake UDP packet sent by peer A to peer B (unencrypted Noise IX first message, per `handshake_manager.go` `beginHandshake`).
2. After A and B complete the handshake and a `HostInfo` exists in B's hostmap keyed by A's vpn address, the attacker resends the identical captured stage-1 packet to B's listening UDP socket, but from an attacker-controlled source address/port.
3. B's `HandleIncoming` routes this as a new stage-1 (`MessageCounter == 1`, `RemoteIndex == 0`), reaching `beginHandshake` → `CheckAndComplete`, which matches the byte-identical cached `HandshakePacket[handshakePacketStage0]` and returns `ErrAlreadySeen` for A's existing `HostInfo`.
4. `handleCheckAndCompleteError` invokes `existing.SetRemoteIfPreferred(f.hostMap, via)` with `via.UdpAddr` set to the attacker's address; per the logic in `hostmap.go`, if A's tunnel currently has no valid remote, or the attacker's address matches a `preferred_ranges` CIDR, B's stored remote for A's tunnel is updated to the attacker's address.
5. B now sends subsequent tunnel traffic destined for A to the attacker's address instead.

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

**File:** handshake_manager.go (L1104-1113)
```go
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

**File:** hostmap.go (L787-823)
```go
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
