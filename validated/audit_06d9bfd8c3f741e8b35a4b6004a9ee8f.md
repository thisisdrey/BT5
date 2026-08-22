### Title
Unauthenticated `header.RecvError` packet with spoofed source + guessed/observed RemoteIndex can force tunnel teardown - (outside.go)

### Summary
`handleRecvError` in `outside.go` tears down a live tunnel (`f.closeTunnel(hostinfo)` and `f.handshakeManager.DeleteHostInfo(hostinfo)`) based solely on an unauthenticated UDP packet whose only "proof" is (1) matching the hostinfo's current remote `netip.AddrPort` and (2) carrying the correct 32-bit `RemoteIndex`. Neither of these values is cryptographically bound to the packet - the address is attacker-spoofable UDP source data, and the index is a plaintext field present in the header of every ordinary packet exchanged between the two peers, so it is not secret to anyone who can observe traffic on the path.

### Finding Description
In `readOutsidePackets`, `header.RecvError` is dispatched before any AEAD/decryption step: [1](#0-0) 
`handleRecvError` performs the following checks and then mutates state: [2](#0-1) 

The only "authentication" performed is:
1. `f.acceptRecvErrorConfig.ShouldRecvError(addr)` - an address/ratelimit policy check, not a cryptographic check.
2. `hostinfo := f.hostMap.QueryReverseIndex(h.RemoteIndex)` - a lookup keyed purely on the plaintext `RemoteIndex` field taken directly from the unauthenticated packet header.
3. `hr := hostinfo.GetRemote(); if hr.IsValid() && hr != addr { ... return }` - a check that the claimed source address equals the hostinfo's currently known remote endpoint. This is an equality check on attacker-controllable UDP source data, not a proof of possession of any key material.

If both checks pass, `closeTunnel` and `DeleteHostInfo` execute immediately, with zero AEAD verification, zero certificate check, and no requirement that the sender ever completed a handshake or possesses any session key.

The `RemoteIndex` used as the lookup key is not a secret: it is transmitted in cleartext in the header of every ordinary encrypted `Message`/`Test`/etc. packet sent by the local host to its peer (headers are never encrypted in Nebula; only the payload is AEAD-protected). Consequently, any entity capable of observing a victim's UDP traffic (e.g., on the same LAN/Wi-Fi, at an intermediate network segment, or via traffic analysis) can read the index value and combine it with a spoofed source `IP:port` matching the victim tunnel's current remote address to produce a `RecvError` packet that passes both checks.

### Impact Explanation
An attacker who can (a) observe network traffic between the two Nebula peers to learn the plaintext `RemoteIndex`, and (b) spoof UDP packets with the exact current remote address of the victim's tunnel, can force unauthenticated teardown of that tunnel (`closeTunnel` + `handshakeManager.DeleteHostInfo`) with a single crafted packet and no cryptographic material. This is a remote, unauthenticated denial-of-service against an established tunnel - state mutation occurs without any AEAD-verified traffic ever being required from the attacker.

### Likelihood Explanation
Feasibility depends heavily on attacker positioning:
- If the attacker is purely off-path with no visibility into the victim's traffic, they must blind-guess the 32-bit `RemoteIndex`, which is a 1-in-2^32 chance per attempt and rate-limited/gated by `acceptRecvErrorConfig.ShouldRecvError`, making brute force impractical.
- If the attacker has any traffic-observation capability (shared network segment, upstream network position, etc.) - which is a step beyond the "unprivileged, no MITM" precondition typically assumed - the index is trivially obtained from cleartext headers, making the attack fully reliable, limited only by the ability to spoof UDP source address/port to match the victim's current remote endpoint.

Because the question's stated preconditions ("attacker knows/guesses the index and can spoof the exact current remote UDP address") already assume this capability is met, the exploit is deterministic given those preconditions - the code performs no cryptographic check beyond that.

### Recommendation
Do not allow `RecvError` to trigger `closeTunnel`/`DeleteHostInfo` based solely on address-equality and an unauthenticated index lookup. Options:
- Require that any peer-initiated teardown be authenticated (e.g., only honor `RecvError` if it can be tied to material derived from the current session, or require it be sent under the current session's transport encryption from the encrypted-message path rather than as a bare unauthenticated header type).
- At minimum, treat `RecvError` as a *hint* to probe/re-handshake rather than an authority to immediately delete hostinfo state, and require corroborating evidence (e.g., repeated confirmed decrypt failures, or an authenticated confirmation) before tearing down.

### Proof of Concept
Unit test plan (in `outside_test.go` or similar):
1. Establish two `Interface`s with a real handshake so a `HostInfo` exists with a known `localIndexId` and `GetRemote()` address `A`.
2. From a third "attacker" socket, craft a raw UDP packet using `header.Encode(..., header.RecvError, 0, index, 0)` with `index` set to the victim's real `localIndexId` (simulating traffic observation) and send it with a spoofed source address equal to `A` (simulating source-spoofing) directly into `f.readOutsidePackets`/`f.handleRecvError` without ever performing a handshake or possessing any key material.
3. Assert that prior to the fix, `hostMap.QueryHostInfo`/`QueryReverseIndex` for that hostinfo returns `nil` afterward (i.e., `closeTunnel` and `handshakeManager.DeleteHostInfo` were invoked) even though no AEAD-authenticated packet was ever sent by the attacker.
4. After remediation, assert that the hostinfo is NOT deleted (or is only re-probed) from a single unauthenticated `RecvError`, i.e., `closeTunnel` must not be reachable purely from address/index equality without cryptographic proof of session ownership.

### Citations

**File:** outside.go (L81-84)
```go
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
