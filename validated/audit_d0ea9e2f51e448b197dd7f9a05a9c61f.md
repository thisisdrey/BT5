### Title
Unauthenticated `RecvError` messages let an attacker force tunnel teardown via spoofable source-address check - (File: outside.go)

### Summary
The Halborn finding on `Treasury.premiumIncome` is a missing-authorization bug: a state-mutating function that should only be callable by a privileged component (`policyCenter`) instead accepted input from *any* caller, letting an attacker corrupt state used for a high-value decision (reporter reward) and drain funds. The structurally equivalent pattern in `nebula--022` is `Interface.handleRecvError`, which mutates critical tunnel state (tears down an established tunnel and deletes its hostinfo) based on an unauthenticated, unencrypted `RecvError` packet, using only a spoofable UDP source-address comparison instead of any cryptographic identity check.

### Finding Description
`RecvError` is one of the two message types dispatched *before* any decryption or peer-certificate verification in `Interface.readOutsidePackets`: [1](#0-0) 

`handleRecvError` is the handler for this unauthenticated packet type: [2](#0-1) 

The only "authentication" performed is:
```go
hr := hostinfo.GetRemote()
if hr.IsValid() && hr != addr {
    // reject
}
f.closeTunnel(hostinfo)
f.handshakeManager.DeleteHostInfo(hostinfo)
```
This is not a cryptographic proof of identity — it is a comparison against a UDP source `netip.AddrPort`, which is attacker-controllable (UDP source spoofing, or simply knowing/guessing the current remote `ip:port`, which is often visible/predictable on typical networks). Critically, if `hr.IsValid()` is `false` (e.g., the hostinfo hasn't recorded a remote yet, such as during relay-only operation or early in a roam), the address check is skipped entirely, and *any* attacker who supplies the correct 32-bit `RemoteIndex` in the packet header can tear down the tunnel and purge its state — no certificate, no handshake, no CA trust chain involved.

This mirrors the `premiumIncome` bug's essence: a function that should be restricted to a "trusted caller" (in Treasury's case, `policyCenter`; here, the genuine remote peer holding the private key matching its CA-signed certificate) instead performs a cheap, spoofable check and lets an arbitrary network attacker mutate sensitive state (`poolIncome` there, hostmap/tunnel state here).

### Impact Explanation
An attacker with no CA-signed certificate can:
- Send spoofed `RecvError` packets to force `closeTunnel` + `DeleteHostInfo` on victim hostinfos, causing remote state poisoning (deletion of legitimate tunnel/hostmap state) and repeated denial-of-service on established or pending tunnels.
- This is a remote, unauthenticated packet-triggered state mutation reachable from anyone who can reach the UDP listener and knows/guesses a `RemoteIndex`, directly analogous to the unrestricted `premiumIncome` call letting an unauthorized caller corrupt reward-critical state.

### Likelihood Explanation
`RemoteIndex` values are exchanged in cleartext in every packet header (handshake and data-plane), so an attacker positioned to observe any traffic for a given tunnel (or one leg of a relay) can trivially harvest a valid index. UDP source-address spoofing is a well-known, low-cost technique, and the check is skipped altogether when `hr` is not yet set. No cryptographic material or valid certificate is required at any point in this path, making the analog readily reachable by exactly the class of attacker the validation rules target.

### Recommendation
Do not use a bare UDP source-address comparison as authentication for `RecvError`. Options include: requiring an authenticated/keyed value (e.g., an AEAD-protected error tag derived from the established session's keys, as is done for `VerifyRelay`/`Decrypt`) before acting on a `RecvError`, or at minimum always requiring `hr.IsValid()` and treating an unset remote as "cannot process" rather than "skip the check." The nebula changelog already shows a related security-hardening step — `Disable sending recv_error messages when a packet is received outside the allowable counter window` (#1459) — but the acceptance side (`handleRecvError`) still relies solely on address matching rather than cryptographic proof of peer identity.

### Proof of Concept
1. Attacker passively observes (or is relayed) UDP traffic for a live nebula tunnel between `me` and `them` and records `RemoteIndex` from any packet header, and/or targets a hostinfo whose `remote` (`hr`) has not yet been set (e.g., relay-only or freshly created pending hostinfo).
2. Attacker crafts a minimal `RecvError` header packet (`header.Encode` with `Type=header.RecvError`, `RemoteIndex=<observed index>`), as constructed in `sendRecvError`: [3](#0-2) 
3. Attacker sends this UDP packet to the victim's listener from an arbitrary/spoofed source address.
4. If `hr.IsValid()` is false, or the attacker's source matches/spoofs `hr`, `handleRecvError` calls `f.closeTunnel(hostinfo)` and `f.handshakeManager.DeleteHostInfo(hostinfo)`, tearing down the tunnel with no certificate or handshake ever presented by the attacker — analogous to an unauthenticated caller mutating `poolIncome` in the original Halborn finding to corrupt reward-critical contract state.

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
