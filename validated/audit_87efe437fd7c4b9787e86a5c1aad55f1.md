## Analysis

The Napier bug's root cause is that an **unauthenticated/unauthorized third party can trigger a state-mutating action targeting a victim's account/session**, forcing an unwanted, damaging state change without the victim's consent (Bob resets Alice's `lscales`/`unclaimedYields` via `issue(to=Alice, ...)`).

The strongest reachable analog in Nebula is `Interface.handleRecvError` in `outside.go`, which processes the unauthenticated `header.RecvError` packet type and can force-close another pair's already-established, cert-verified tunnel.

### Title
Unauthenticated `RecvError` packets let a third party force-close another peer's established tunnel via spoofed source address and a leaked plaintext index - (File: outside.go)

### Summary
`readOutsidePackets` dispatches `header.RecvError` packets directly to `f.handleRecvError` before any handshake, decryption, or certificate check occurs, since `RecvError` is one of the two packet types explicitly handled as "Unencrypted packets". [1](#0-0) 

`handleRecvError` looks up the target hostinfo purely by the plaintext `h.RemoteIndex` field in the packet header, and its only "authentication" is comparing the packet's UDP source `addr` against the last-known remote address stored on the hostinfo: [2](#0-1) 

### Finding Description
`RecvError` is a bare, unauthenticated 12-byte UDP header (no noise handshake, no certificate, no HMAC/AEAD tag) — it is handled at the very top of `readOutsidePackets`, prior to any lookup of `ConnectionState` or decryption: [3](#0-2) 

The only gate applied to an incoming `RecvError` is `f.acceptRecvErrorConfig.ShouldRecvError(addr)`, an address-allow-list config, and then a comparison of the sender's UDP source address to the value cached in `hostinfo.GetRemote()`: [4](#0-3) 

Both of the values an attacker needs are attacker-obtainable without ever holding a CA-signed certificate or completing a handshake:
- `h.RemoteIndex` (the victim's `localIndexId`) is sent in **every** packet header in plaintext, including handshake and encrypted message headers, so any network observer sees it. `generateIndex` produces this value, but it does not need to be guessed — it is directly visible on the wire.
- The UDP source address check is a simple field comparison, not a cryptographic proof of origin; UDP source addresses are attacker-forgeable at the network layer (classic UDP spoofing), and the code itself flags this exact scenario in a log line ("Someone spoofing recv_errors?") without actually preventing it when the spoof happens to match.

If the forged packet's source matches the currently recorded remote for that hostinfo (which an on-path or off-path spoofing attacker can arrange), `handleRecvError` unconditionally tears the tunnel down on both the main and pending hostmaps: [5](#0-4) 

This lets an unauthenticated attacker — with no CA-signed certificate, no completed handshake, and no valid session key — destroy an already-established, mutually-authenticated tunnel between two legitimate peers, exactly mirroring the Napier pattern of "anyone can trigger a state-changing action against a victim's already-established position without their authorization."

### Impact Explanation
Forcing `closeTunnel` on a legitimate, established session:
- Destroys the hostinfo, `ConnectionState`, and relay state (`unlockedDeleteHostInfo` disestablishes relay-for entries), causing loss of in-flight application traffic queued for that tunnel.
- Forces a costly re-handshake between the victims, which is itself state that a third party should never be able to unilaterally trigger.
- Can be repeated to produce a persistent remote denial-of-service against a specific pair of authenticated peers, without the attacker ever needing to be a member of the mesh.

### Likelihood Explanation
The attacker needs to observe (via any point on the network path, or historically via traffic capture) a single header from the victim's tunnel to learn `RemoteIndex`, and must be able to spoof or otherwise get the sender's UDP source address to match the responder's currently recorded remote. This is a well-known UDP amplification/spoofing primitive, and the code's own comment ("Someone spoofing recv_errors?") shows the maintainers were aware address spoofing was the exact residual risk here but only log it rather than cryptographically prevent it.

### Recommendation
Do not allow an unauthenticated 12-byte `RecvError` packet to unilaterally tear down an already fully-handshaked, cert-verified tunnel. At minimum:
- Require `RecvError` handling to additionally verify some proof tied to the current session (e.g., require it be authenticated/encrypted under the existing session key, or require a matching nonce/token established during the handshake) rather than relying solely on a plaintext index plus a spoofable source-address match.
- Rate-limit / require corroboration (e.g., only honor `RecvError` if outbound traffic to that index also fails) before tearing down the tunnel.

### Proof of Concept
1. Passively observe (or otherwise learn) the victim responder's `localIndexId` from any Nebula packet header exchanged between victim-A and victim-B (the field is always in plaintext, per `header.Encode`/`header.Parse`).
2. Craft a 12-byte UDP packet: `header.Encode(buf, header.Version, header.RecvError, 0, victimLocalIndex, 0)`.
3. Spoof the UDP source address to match victim-B's currently known remote `ip:port` (the address currently stored in `hostinfo.remote` on victim-A), and send it to victim-A's listen port.
4. `readOutsidePackets` routes the packet to `handleRecvError` without any handshake/decryption; the address check passes because the spoofed source matches; `f.closeTunnel(hostinfo)` and `f.handshakeManager.DeleteHostInfo(hostinfo)` execute, destroying the tunnel between victim-A and victim-B, with neither of them being the attacker's certificate holder.

### Citations

**File:** outside.go (L75-84)
```go
	// Unencrypted packets
	switch h.Type {
	case header.Handshake:
		f.handshakeManager.HandleIncoming(via, packet, h)
		return

	case header.RecvError:
		f.handleRecvError(via.UdpAddr, h)
		return
	}
```

**File:** outside.go (L541-574)
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
```
