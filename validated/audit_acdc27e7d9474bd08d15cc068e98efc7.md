### Title
Unauthenticated `RecvError` packet allows any attacker to remotely tear down a peer's tunnel — ([File: outside.go])

### Summary
Nebula's `readOutsidePackets` dispatches `header.RecvError` packets to `f.handleRecvError` before any certificate/handshake verification is performed and before the AEAD/decryption stage. `handleRecvError` looks up a live tunnel purely by the attacker-controlled `RemoteIndex` field carried in the plaintext header and, if the source UDP address happens to match the tunnel's currently known remote, deletes the `HostInfo` state (closing the tunnel) — no cryptographic proof of identity, no valid CA-signed certificate, and no completed handshake are required from the caller.

### Finding Description
In `readOutsidePackets`, the packet type/subtype is parsed and, before any hostinfo lookup or decryption, unauthenticated header types are dispatched directly: [1](#0-0) 

`header.RecvError` is handled by `f.handleRecvError(via.UdpAddr, h)`, which is invoked purely from the parsed header — no `ConnectionState`, no cert, no AEAD tag check on this branch: [2](#0-1) 

The only "authentication" performed is:
1. A rate-limit gate `ShouldRecvError(addr)` (a config-driven allow/rate check, not cryptographic).
2. A comparison of the observed source UDP address against `hostinfo.GetRemote()` — but this check is only enforced `if hr.IsValid()`, and even when enforced it is trivially satisfiable because UDP source addresses are attacker-controlled/spoofable and are also learned/updated opportunistically elsewhere in the code (roaming, `SetRemote`), so an off-path or address-spoofing attacker who merely knows/guesses a target's current UDP endpoint and the numeric `RemoteIndex` of an existing tunnel can trigger `f.closeTunnel(hostinfo)` and `f.handshakeManager.DeleteHostInfo(hostinfo)`.

This closely mirrors the reported bug class: a state-mutating function that is meant to be reachable only through a trusted/authenticated path (in the original report, only the Vault after full validation; here, only a genuine peer that has actually observed the AEAD failure) but is instead exposed to any caller who can reach the entry point without ever proving possession of a valid credential (no CA-signed certificate, no completed Noise handshake). Just as `onAfterRemoveLiquidity` deleted `FeeData` and left victims with zero balance, `handleRecvError` deletes hostinfo/tunnel state and forces victims to re-handshake, achieving a remote, unauthenticated state-poisoning/DoS effect.

### Impact Explanation
An attacker with no certificate and no established tunnel can force termination of any active Nebula tunnel it can address (i.e., knows the victim's UDP endpoint and can observe/guess a live `RemoteIndex`, which is a 32-bit value transmitted in the clear on every packet and thus observable to any network-position or on-path/off-path spoofing attacker). This yields:
- Remote, unauthenticated denial-of-service against a specific mesh tunnel (repeated killing of established tunnels).
- Loss of established relay/lighthouse learned state for the peer (`f.lightHouse.DeleteVpnAddrs`) if it was the final tunnel, degrading connectivity/discoverability.

This satisfies the "remote state poisoning / remote crash-of-service" impact bar even though it does not directly break confidentiality of already-encrypted traffic.

### Likelihood Explanation
Reaching this code path requires only sending a single unauthenticated UDP packet with `header.RecvError` type and a guessed/observed `RemoteIndex` from an address that matches (or is spoofed to match) the peer's currently known remote endpoint — no certificate exchange or handshake completion is required. `RemoteIndex` values are transmitted unencrypted in every packet header, making them observable to any attacker positioned to see traffic between two nodes, and UDP source-address spoofing is a well-known, low-cost primitive. The `ShouldRecvError` rate-limiting gate reduces but does not eliminate exploitability, since it is a local acceptance policy, not an authentication mechanism.

### Recommendation
- Require the `RecvError` handler to only act on `RemoteIndex`/source combinations that can be tied to information the peer could not have observed passively, or authenticate the `RecvError` message (e.g., AEAD-sign it, or require it be delivered as a normal encrypted `Message`/`Control` packet under the existing tunnel key) instead of accepting a fully plaintext, unauthenticated header-only message.
- Do not allow tunnel teardown decisions to be based solely on UDP source-address equality, since UDP addresses are attacker-controlled/spoofable.
- At minimum, require possession of secret material bound to the specific tunnel (e.g., embed a message authentication code derived from the tunnel's cipher state) before honoring a `RecvError`.

### Proof of Concept
Conceptual PoC (network-level, not code from this repo):
1. Establish two legitimate Nebula peers, A and B, with a completed handshake and active tunnel (`RemoteIndex` for B's view of A is now known/observable in cleartext on the wire).
2. From an attacker-controlled host C (or a spoofed source), send a raw UDP packet to B's listen port with:
   - `header.H.Type = header.RecvError`
   - `header.H.RemoteIndex` = the (observed) index A uses when talking to B
   - Source UDP address spoofed to match A's last known UDP endpoint (or sent while B's remote for that hostinfo is still marked invalid, e.g., right after roaming).
3. B's `readOutsidePackets` dispatches directly to `handleRecvError` without any cert/handshake check, matches the reverse index in `hostmap`, and (per `hr.IsValid()` / source-match logic) proceeds to call `f.closeTunnel(hostinfo)` and `f.handshakeManager.DeleteHostInfo(hostinfo)`.
4. B's tunnel to A is torn down without A's cooperation and without the attacker ever presenting a certificate or completing a handshake, demonstrating unauthenticated remote state poisoning/DoS.

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
