### Title
Unauthenticated stage-1 handshake replay with spoofed source enables UDP reflection/amplification via cached stage-2 response - ([File: handshake_manager.go])

### Summary
`HandshakeManager.beginHandshake` processes any stage-1 packet whose bytes match a previously completed handshake and, via `CheckAndComplete`'s `ErrAlreadySeen` path, unconditionally resends the cached stage-2 response to `via.UdpAddr` — the UDP source address taken directly from the incoming datagram. Since UDP source addresses are attacker-controllable (spoofable) and there is no per-source rate limiting anywhere in the handshake path, an attacker who has observed one legitimate stage-1 packet on the wire can replay it with a spoofed source IP to repeatedly cause the responder to send response packets toward an arbitrary third-party victim address.

### Finding Description
The relevant call chain is `HandleIncoming` → `beginHandshake` → `CheckAndComplete` (returns `ErrAlreadySeen`) → `handleCheckAndCompleteError` → `sendHandshakeResponse`.

- `HandleIncoming` accepts any stage-1 packet (`h.MessageCounter == 1`, `RemoteIndex == 0`) subject only to `AllowUnknownVpnAddr` and then calls `beginHandshake` [1](#0-0) .
- `beginHandshake` runs the Noise machine on the packet bytes (deterministic on packet content, not bound to the UDP source address) and, for a byte-identical replay of a previously-processed stage-1 packet, `CheckAndComplete` finds the match via `bytes.Equal(hostinfo.HandshakePacket[handshakePacket], testHostInfo.HandshakePacket[handshakePacket])` and returns `ErrAlreadySeen` [2](#0-1) .
- `handleCheckAndCompleteError`'s `ErrAlreadySeen` branch resends the cached stage-2 response unconditionally: `if msg := existing.HandshakePacket[handshakePacketStage2]; msg != nil { hm.sendHandshakeResponse(via, msg, existing, true) }` [3](#0-2) .
- `sendHandshakeResponse` sends this cached message directly to `via.UdpAddr` via `f.outside.WriteTo(msg, via.UdpAddr)` with no validation that `via.UdpAddr` is the genuine sender [4](#0-3) .
- `via.UdpAddr` originates from the raw UDP receive path and is not authenticated; UDP allows arbitrary source-address spoofing at the network layer, so an attacker who captures one legitimate stage-1 packet can resend it with a forged source address pointing at a victim.

The `SetRemoteIfPreferred` call in the same branch is gated by `preferred_ranges` matching, so the additional `Test`/`TestRequest` amplification only triggers if the spoofed address falls within a configured preferred range (uncommon for a public third-party victim) [5](#0-4) . However, the stage-2 resend via `sendHandshakeResponse` is unconditional and does not depend on preferred ranges, so it always fires on every replay. There is no per-source-address rate limiter anywhere on this path — a codebase-wide search for rate-limiting primitives applicable to handshake/UDP traffic found none [6](#0-5) .

### Impact Explanation
This allows an unprivileged, unauthenticated attacker to use a Nebula node as a UDP reflector: by replaying one captured, valid stage-1 handshake packet with a spoofed source address, the node will repeatedly emit a stage-2 handshake response toward the spoofed (victim) address at a rate limited only by the attacker's send rate. This is a reflection/amplification DoS against a third party, consistent with "remote crash/wedge"-adjacent network abuse impact categories (spoofed-traffic amplification), and additionally imposes processing overhead on the responder for every replayed packet (Noise processing, hostmap lookups, logging).

### Likelihood Explanation
Preconditions are modest and match the stated threat model: the attacker only needs to observe one legitimate stage-1 packet on the wire (trivial for any on-path or same-network observer, or in some deployments simply by initiating their own handshake and reusing packets structurally) and be able to spoof UDP source addresses (feasible on networks without strict egress/ingress filtering, which is common for UDP reflection attacks generally). The replay itself requires no cryptographic material and is deterministic — `bytes.Equal` on the exact captured bytes guarantees the `ErrAlreadySeen` path is hit every time, so the attack is fully repeatable at attacker-controlled rate.

### Recommendation
Add per-source-address (and/or per-vpnAddr) rate limiting to the `ErrAlreadySeen` retransmit path in `handleCheckAndCompleteError` before calling `sendHandshakeResponse`, so that cached stage-2 resends are capped independent of how fast an attacker replays the captured packet. Consider also bounding response size relative to request size, and/or requiring some return-routability signal (e.g., only resending to the currently recorded/verified remote address for that hostinfo rather than blindly trusting the packet's `via.UdpAddr`) before honoring a replayed stage-1 for retransmission purposes.

### Proof of Concept
Integration test plan (extends existing e2e test style in `e2e/handshake_manager_test.go`):
1. Complete a legitimate handshake between `me` and `them`, capturing the exact stage-1 packet bytes (`msg1`) sent from `me` to `them`.
2. Simulate an attacker: repeatedly call `theirControl.InjectUDPPacket` with the same `msg1` bytes, but wrapped in `udp.Packet{From: spoofedVictimAddr, To: theirUdpAddr, Data: msg1.Data}` for many spoofed `From` addresses/varying rates.
3. Assert that for each injection, `theirControl.GetFromUDP` produces an outbound stage-2 response packet addressed to the spoofed `From` value.
4. Measure and assert an amplification factor: `(bytes sent in stage-2 response) * (number of accepted replays per unit time)` vs. `(bytes attacker sent)`, expecting the test to demonstrate unbounded amplification (no per-source cap) — the assertion should fail against the current code (no rate limiter) and pass once a rate limiter is added, confirming the fix.

### Citations

**File:** handshake_manager.go (L172-184)
```go
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

**File:** handshake_manager.go (L437-444)
```go
	existingHostInfo, found := hm.mainHostMap.Hosts[hostinfo.vpnAddrs[0]]
	if found && existingHostInfo != nil {
		// Is it just a delayed handshake packet? Check every hostinfo we hold for this address.
		for _, testHostInfo := range hm.mainHostMap.unlockedGetHostList(hostinfo.vpnAddrs[0]) {
			if bytes.Equal(hostinfo.HandshakePacket[handshakePacket], testHostInfo.HandshakePacket[handshakePacket]) {
				return testHostInfo, ErrAlreadySeen
			}
		}
```

**File:** handshake_manager.go (L1072-1079)
```go
	if !via.IsRelayed {
		fields := append(logFields, "from", via)
		err := f.outside.WriteTo(msg, via.UdpAddr)
		if err != nil {
			f.l.Error("Failed to send handshake message", append(fields, "error", err)...)
		} else {
			f.l.Info("Handshake message sent", fields...)
		}
```

**File:** handshake_manager.go (L1094-1113)
```go
// handleCheckAndCompleteError handles errors from CheckAndComplete.
// This only fires from the responder-side beginHandshake path, after the
// peer cert has been validated and ConnectionState populated, so peerCert
// is always non-nil for the cases that log it.
func (hm *HandshakeManager) handleCheckAndCompleteError(err error, existing, hostinfo *HostInfo, via ViaSender) {
	f := hm.f
	peerCert := hostinfo.ConnectionState.peerCert
	hsFields := m{"stage": uint64(1), "style": header.SubTypeName(header.Handshake, header.HandshakeIXPSK0)}

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
