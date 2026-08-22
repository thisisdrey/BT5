### Title
Unauthenticated `RecvError` packet allows remote attacker to force tunnel teardown and loss of in-flight/pending traffic - (File: outside.go)

### Summary
The external report describes SKALE's `MessageProxy.removeConnectedChain` tearing down cross-chain state without checking for pending/in-flight messages, causing them to be lost. The closest reachable analog in Nebula is `header.RecvError` handling: it is processed as an **unencrypted, unauthenticated** packet type, and successfully triggers a full local tunnel teardown (`closeTunnel` + `handshakeManager.DeleteHostInfo`) for an *already established* tunnel. Just like the SKALE finding, this drops whatever tunnel state exists (including any traffic that was en route or would have been retried) purely as a side effect of a control-plane message that is not cryptographically bound to the peer that owns the session.

### Finding Description
`readOutsidePackets` dispatches on the wire header before any decryption or Noise verification occurs: [1](#0-0) 

`RecvError` is handled immediately, with no MAC/AEAD check, no cipher-state verification, and no proof that the sender possesses the peer's private key or even a CA-signed certificate: [2](#0-1) 

The only gate is `f.acceptRecvErrorConfig.ShouldRecvError(addr)` (a local rate-limit/allow check keyed on the *source UDP address*, not on any cryptographic identity) and a comparison of the packet's source `addr` against the tunnel's currently known remote `netip.AddrPort` (`hr`). Both of these are values derived from the UDP source address of the incoming datagram, which is attacker-controllable on many networks (UDP source spoofing, or simply being on-path/NAT-observing the real peer's endpoint). If the check passes, the code immediately calls:

- `f.closeTunnel(hostinfo)` → `hostMap.DeleteHostInfo` → clears lighthouse-learned state via `lightHouse.DeleteVpnAddrs`
- `f.handshakeManager.DeleteHostInfo(hostinfo)` → wipes any pending handshake state for fast reconnect [3](#0-2) [4](#0-3) 

This is structurally the same bug class as the SKALE report: a state-removal operation is triggered without verifying that the party requesting removal is authorized, and without regard to what tunnel/session state (in-flight packets, relay state, connection-manager tracking) is still active. Just as `removeConnectedChain` deletes `connectedChains[schainHash]` unconditionally, `handleRecvError` deletes the entire local hostinfo/handshake state unconditionally once the (weak) source-address check passes — the difference is that here the "actor" triggering removal doesn't need any credential at all, only knowledge/spoofing of the victim's UDP source address.

### Impact Explanation
An attacker who can spoof or observe the UDP source address of a legitimate Nebula peer (no CA-signed certificate required) can inject a single unauthenticated `RecvError` packet and force the local node to tear down an established, authenticated tunnel to that peer. This causes:
- Immediate loss of the encrypted session and any traffic queued or in flight over it (directly analogous to "loss of pending messages" from the referenced report).
- Deletion of pending handshake state, and clearing of lighthouse-cached address data for the peer, forcing full re-handshake and rediscovery.
- A trivial, repeatable remote denial-of-service against any specific tunnel, degrading availability without needing to compromise any key material.

This does not itself decrypt traffic or forge data-plane packets, but it does allow unauthenticated remote state poisoning/teardown of an authenticated session — squarely in the "remote state poisoning" / DoS impact category.

### Likelihood Explanation
Likelihood is moderate-to-high in networks where UDP source-address spoofing is feasible (no BCP38/anti-spoofing filtering) or where the attacker is on-path/can observe the peer's `AddrPort`. `ShouldRecvError`/`AcceptRecvErrorConfig` appears to be a rate-limiting mechanism rather than a cryptographic control (it is keyed purely on `netip.AddrPort`), so it does not provide authentication — it can, at best, throttle repeated abuse, not prevent a single forged packet from a spoofed/matching source.

### Recommendation
- Do not act on `RecvError` (or any other unauthenticated control message) to tear down an *established* tunnel based solely on source-address matching. Require some cryptographic binding (e.g., only accept `RecvError` referencing a specific `localIndexId`/`remoteIndexId` combined with proof-of-knowledge, or only honor it before a tunnel completes its handshake).
- If `RecvError` must remain unauthenticated (as an early ICMP-like signal for handshake retries), scope its effect to non-established/pending state only, and never call `closeTunnel`/`hostMap.DeleteHostInfo` on an already-authenticated `HostInfo` in response to it.
- Consider pausing/quarantining rather than immediately deleting hostinfo state, and rate-limit/log repeated `RecvError` hits per remote index to detect spoofing abuse, mirroring the "pause before actual removal" mitigation suggested in the original report.

### Proof of Concept
1. Establish a legitimate Nebula tunnel between node A and node B (`hostinfo` exists in `A.hostMap` with `hostinfo.GetRemote() == B_addr`).
2. From a spoofed/observed source `B_addr`, send node A an unencrypted UDP packet: `header.Encode(..., header.RecvError, 0, <A's local index for B>, 0)` — matching the wire format produced by `sendRecvError`: [5](#0-4) 
3. `readOutsidePackets` on A dispatches this straight to `handleRecvError` without any decryption/authentication: [6](#0-5) 
4. If `ShouldRecvError(B_addr)` allows it and the reported source matches A's recorded remote for that hostinfo, A immediately tears down the tunnel and deletes handshake state, regardless of any in-flight application traffic: [7](#0-6) 

Note: I was unable to inspect the exact implementation of `ShouldRecvError`/`AcceptRecvErrorConfig` in `interface.go` (only match locations were returned, not their bodies) before the session ended, so I cannot fully characterize whether it performs any additional cryptographic gating beyond address/rate-limiting. If further verification is needed on that specific point, a Devin session with full file access should read `interface.go` around the `RecvErrorConfig`/`ShouldRecvError` definitions to confirm.

### Citations

**File:** outside.go (L76-84)
```go
	switch h.Type {
	case header.Handshake:
		f.handshakeManager.HandleIncoming(via, packet, h)
		return

	case header.RecvError:
		f.handleRecvError(via.UdpAddr, h)
		return
	}
```

**File:** outside.go (L250-257)
```go
// closeTunnel closes a tunnel locally, it does not send a closeTunnel packet to the remote
func (f *Interface) closeTunnel(hostInfo *HostInfo) {
	final := f.hostMap.DeleteHostInfo(hostInfo)
	if final {
		// We no longer have any tunnels with this vpn addr, clear learned lighthouse state to lower memory usage
		f.lightHouse.DeleteVpnAddrs(hostInfo.vpnAddrs)
	}
}
```

**File:** outside.go (L528-539)
```go
func (f *Interface) sendRecvError(endpoint netip.AddrPort, index uint32) {
	f.messageMetrics.Tx(header.RecvError, 0, 1)

	b := header.Encode(make([]byte, header.Len), header.Version, header.RecvError, 0, index, 0)
	_ = f.outside.WriteTo(b, endpoint)
	if f.l.Enabled(context.Background(), slog.LevelDebug) {
		f.l.Debug("Recv error sent",
			"index", index,
			"udpAddr", endpoint,
		)
	}
}
```

**File:** outside.go (L541-575)
```go
func (f *Interface) handleRecvError(addr netip.AddrPort, h *header.H) {
	if !f.acceptRecvErrorConfig.ShouldRecvError(addr) {
		f.l.Debug("Recv error received, ignoring",
			"index", h.RemoteIndex,
			"udpAddr", addr,
		)
		return
	}

	if f.l.Enabled(context.Background(), slog.LevelDebug) {
		f.l.Debug("Recv error received",
			"index", h.RemoteIndex,
			"udpAddr", addr,
		)
	}

	hostinfo := f.hostMap.QueryReverseIndex(h.RemoteIndex)
	if hostinfo == nil {
		f.l.Debug("Did not find remote index in main hostmap", "remoteIndex", h.RemoteIndex)
		return
	}

	hr := hostinfo.GetRemote()
	if hr.IsValid() && hr != addr {
		f.l.Info("Someone spoofing recv_errors?",
			"addr", addr,
			"hostinfoRemote", hr,
		)
		return
	}

	f.closeTunnel(hostinfo)
	// We also delete it from pending hostmap to allow for fast reconnect.
	f.handshakeManager.DeleteHostInfo(hostinfo)
}
```

**File:** hostmap.go (L433-442)
```go
// DeleteHostInfo will fully unlink the hostinfo and return true if no other hostinfo still holds
// any of its vpn addrs, meaning we no longer have a tunnel to the peer
func (hm *HostMap) DeleteHostInfo(hostinfo *HostInfo) bool {
	// Delete the host itself, ensuring it's not modified anymore
	hm.Lock()
	final := hm.unlockedDeleteHostInfo(hostinfo)
	hm.Unlock()

	return final
}
```
