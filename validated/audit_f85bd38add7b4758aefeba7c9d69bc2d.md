### Title
Unauthenticated `RecvError` packets allow remote-index-based tunnel teardown without any CA-signed certificate - (File: outside.go)

### Summary
`RecvError` is one of the two packet types handled entirely before decryption/authentication in `Interface.readOutsidePackets` (the "Unencrypted packets" switch), the other being `Handshake`. [1](#0-0)  Anyone who can put a UDP packet on the wire with the right `RemoteIndex` value — with no certificate, no completed handshake, and no decryption capability — can trigger `handleRecvError`, which looks up a live `HostInfo` purely by that attacker-supplied index and, under a comparably weak condition, tears the tunnel down. [2](#0-1) 

### Finding Description
This is the closest reachable analog to the original Insure Vault finding: a component that is supposed to only be actionable by an authorized/authenticated party instead lets an *unauthenticated* actor trigger a state-changing action (there, moving another user's approved tokens; here, killing another host's already-established tunnel) based only on an identifier value (`_beneficiary`/allowance there, `RemoteIndex`/source address there) rather than on proof of identity.

`readOutsidePackets` parses the header and dispatches `header.RecvError` to `f.handleRecvError` before any `ConnectionState.Decrypt`/`VerifyRelay` call runs, i.e. before the packet has been cryptographically tied to any certificate holder: [1](#0-0) 

`handleRecvError` then:
1. Gates only on a local, non-cryptographic policy check (`acceptRecvErrorConfig.ShouldRecvError(addr)`), which is address-based rate/allow logic, not an authentication check.
2. Looks up the victim `HostInfo` solely by the attacker-controlled `h.RemoteIndex` field from the packet header via `f.hostMap.QueryReverseIndex(h.RemoteIndex)`.
3. Compares the packet's (spoofable, since this is UDP and pre-decryption) source address to the currently known remote address of that `HostInfo`; if the stored remote is *not yet valid* (e.g., mid-handshake or roaming) or *matches* the spoofed source, it proceeds to `f.closeTunnel(hostinfo)` and deletes the handshake-manager state too. [3](#0-2) 

Nothing in this path verifies a certificate, checks a MAC/AEAD tag, or otherwise proves the sender is the legitimate remote peer for that index. The only defense is the `hr != addr` "someone spoofing recv_errors?" heuristic, which is bypassed whenever the true remote's UDP address is known/observable (on-path attacker, shared NAT, or simply the fact that endpoint IP:port pairs are frequently visible/guessable in a P2P mesh) or whenever the victim's remote address is not yet pinned (`!hr.IsValid()`), which is exactly the window during handshake/roaming.

### Impact Explanation
An attacker with **no CA-signed certificate at all** can force teardown of any live tunnel whose `RemoteIndex` they can observe or guess, causing:
- Remote state poisoning: the victim's `HostInfo` and pending handshake state are deleted (`f.closeTunnel`, `f.handshakeManager.DeleteHostInfo`) based on an unauthenticated packet.
- Denial of service against arbitrary mesh peers, repeatable at will, without ever completing a handshake or possessing key material — directly analogous to the root cause in the original finding: a privileged action gated on a weak/observable identifier rather than on proof of authorization.

### Likelihood Explanation
Indices are 32-bit values generated per handshake and echoed back in cleartext header fields of subsequent handshake/data packets, so any on-path or same-broadcast-domain observer can harvest a live `RemoteIndex`/address pair without ever being a participant in the tunnel. Combined with UDP source spoofing (feasible against many networks, and trivially bypassable when the victim's remote is still `!IsValid()`), this makes the attack practical for a network-adjacent unauthenticated attacker, not merely a "hand-wavy hypothetical."

### Recommendation
Do not act on `RecvError` (or any other pre-authentication signal) using only the attacker-supplied index and unauthenticated source address. Require the recv-error acknowledgment to be authenticated (e.g., MAC'd/derived from the existing session's cipher state, or require a subsequent, cryptographically verified control message from the actual remote) before tearing down a `HostInfo`, mirroring how `handleOutsideRelayPacket`/data-plane packets are only trusted after `Decrypt`/`VerifyRelay` succeeds.

### Proof of Concept
1. Observe (via passive sniffing, or by being a peer target for a `Handshake`) the `RemoteIndex` value nebula assigned to a target tunnel between victim `A` and `B` (visible in cleartext header fields of handshake/data packets).
2. Craft a UDP packet with header `Type = header.RecvError`, `RemoteIndex` set to the value learned in step 1, and a spoofed source address matching `B`'s known UDP endpoint (or send it while `A`'s stored remote for that `HostInfo` is not yet `IsValid()`, e.g., during a pending handshake/roam window).
3. Send it to `A` without ever presenting a certificate or completing any handshake.
4. `A`'s `readOutsidePackets` routes it straight to `handleRecvError` (pre-decryption path), which finds the `HostInfo` by index and calls `f.closeTunnel(hostinfo)` plus `f.handshakeManager.DeleteHostInfo(hostinfo)`, destroying the tunnel state. [3](#0-2)

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
