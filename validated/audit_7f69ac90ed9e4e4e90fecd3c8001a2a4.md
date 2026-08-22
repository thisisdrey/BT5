### Title
Unauthenticated UDP-source poisoning of a peer's tracked remote address via replayed handshake bytes - (File: `handshake_manager.go`, `hostmap.go`)

### Summary
The Sherlock finding's root cause is a state-update that fires unconditionally on a *recognized event* without checking whether the event represented a genuinely new/successful outcome, letting a stale/default value be trusted downstream. The closest reachable analog in nebula is `HandshakeManager.CheckAndComplete`'s `ErrAlreadySeen` path combined with `HostInfo.SetRemoteIfPreferred`: on seeing a byte-identical retransmitted handshake packet, the code unconditionally treats the *sender of that packet* (`via.UdpAddr`) as a legitimate roam candidate and updates the tracked remote UDP address, without any check that the UDP source is actually the real peer.

### Finding Description
`CheckAndComplete` compares only the raw handshake packet bytes to detect a "delayed"/retransmitted handshake: [1](#0-0) 

When this matches, `handleCheckAndCompleteError` calls `existing.SetRemoteIfPreferred(f.hostMap, via)`, where `via` carries the UDP source address the (possibly forged) packet arrived from — not a cryptographically-verified peer identity: [2](#0-1) 

`SetRemoteIfPreferred` then unconditionally updates the hostinfo's tracked remote address and roam bookkeeping whenever `via.UdpAddr` falls in a "preferred range", with no re-verification that this UDP source is the actual established peer: [3](#0-2) 

Because UDP has no source-address integrity and the handshake-message-1 bytes contain only public Noise/IX material (ephemeral public key + certificate payload, no secret), an attacker who observes/captures one legitimate stage-0 handshake packet in transit can replay those exact bytes from a spoofed UDP source address that lies inside a configured `preferred_ranges` CIDR (e.g., attacker on the same LAN segment forging a source IP within that subnet). The `bytes.Equal` retransmit-detection path fires, and the state update in `SetRemoteIfPreferred` blindly trusts the packet's UDP source and re-points the hostinfo's remote address and roam history to it — mirroring the report's core flaw: a bookkeeping update proceeds on the assumption of "success"/authenticity without confirming it actually happened for that specific packet/sender.

### Impact Explanation
If the tracked remote address is poisoned, subsequent data-plane traffic to that peer is transmitted toward the attacker-controlled/spoofed address rather than the genuine peer, causing traffic redirection/blackholing for that tunnel (a form of remote state poisoning of hostmap/roaming metadata reachable without holding a CA-signed certificate). This matches the "remote state poisoning" category called out as acceptable impact.

### Likelihood Explanation
Exploitation requires the attacker to (a) capture one legitimate, byte-identical handshake stage-0 packet (feasible for an on-path/local-network observer, since the packet is unencrypted UDP) and (b) be able to source-spoof a UDP packet into an address range configured as `preferred_ranges`. This is a realistic but non-trivial on-path/spoofing precondition — no valid certificate, private key, or successful handshake completion is required by the attacker, satisfying the "no CA-signed certificate" constraint. Likelihood is moderate: it depends on local-network spoofing capability and a `preferred_ranges` configuration, both plausible in typical nebula deployments (e.g., private/local subnets are commonly marked preferred).

### Recommendation
Do not let the `ErrAlreadySeen` retransmit-detection path drive remote-address/roam updates purely from packet-byte equality. Require independent confirmation that the sender is authorized before mutating `HostInfo.remote`/`lastRoam` — e.g., only trust the roam candidate address after it has demonstrated possession of the session by completing (or re-authenticating) a fresh handshake/keyed exchange, or bind the retransmit-detection logic to a source that has already been validated for this tunnel (such as matching against `hostinfo.remotes`/`lastRoamRemote` history plus a freshness/challenge check) rather than accepting any UDP source whose address happens to fall in a preferred CIDR.

### Proof of Concept
Conceptual (not verified end-to-end due to lack of execution environment):
1. Peer `A` and `B` complete a normal handshake; `A` sends stage-0 handshake packet `P` to `B`.
2. Attacker on the local network captures `P` (unencrypted UDP, public Noise IX message).
3. Attacker crafts a UDP packet with source IP spoofed to an address inside `B`'s `lighthouse`/`preferred_ranges` CIDR and payload identical to `P`, and sends it to `B`.
4. `B`'s `HandshakeManager.CheckAndComplete` finds the byte-identical packet in `existingHostInfo.HandshakePacket`, returns `ErrAlreadySeen`.
5. `handleCheckAndCompleteError` calls `existing.SetRemoteIfPreferred(hostMap, via)` with the spoofed `via.UdpAddr`; since it is in `preferred_ranges`, `B` updates `A`'s tracked remote address to the spoofed address.
6. `B`'s subsequent outbound traffic for that tunnel is sent to the spoofed address instead of the real peer `A`.

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
