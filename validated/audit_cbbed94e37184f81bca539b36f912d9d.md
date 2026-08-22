### Title
Unauthenticated `RecvError` packet allows spoofed teardown of an active, fully-established tunnel - (File: outside.go)

### Summary
`handleRecvError` in `outside.go` tears down an active tunnel (`f.closeTunnel(hostinfo)` + `f.handshakeManager.DeleteHostInfo(hostinfo)`) based on an unauthenticated, unencrypted `header.RecvError` packet whose only "proof of legitimacy" is that its source UDP address happens to match the hostinfo's currently recorded remote address. This mirrors the Teller `commitCollateral` root cause: a state-mutating operation that should only be reachable in a pre-established/pending state is instead reachable against an already-active resource, with the only gate being an easily-spoofed, non-cryptographic check.

### Finding Description
`RecvError` is dispatched straight out of `readOutsidePackets` before any decryption or index/cert verification is required: [1](#0-0) 

`handleRecvError` then looks the sender up purely by the claimed `RemoteIndex` in the header and by UDP source address equality — there is no signature, no nonce, no proof tied to the noise/handshake session: [2](#0-1) 

Specifically:
- `hostinfo := f.hostMap.QueryReverseIndex(h.RemoteIndex)` — `RemoteIndex` is a plaintext 32-bit field taken directly from the unauthenticated header (`header/header.go` `Parse`), so an attacker only needs to guess/observe a valid index.
- The "anti-spoof" check is `hr.IsValid() && hr != addr` — i.e. it only rejects the packet if the source address does *not* match the hostinfo's `GetRemote()`. If the attacker can put the victim's real UDP source address on the wire (trivial for on-path attackers, common on unauthenticated UDP, or simply by being co-located/spoofing on many networks) or if the tunnel's remote address is otherwise known/predictable, this check passes.
- Once passed, the function unconditionally calls `f.closeTunnel(hostinfo)` — which fully removes the hostinfo from the hostmap — on a **fully active, established tunnel**, with no check that the tunnel is in any particular state (pending vs. established) and no cryptographic tie to the actual handshake keys.

This is functionally identical in structure to the Teller bug: `commitCollateral` mutated state on an object (`bidId`) without verifying whether that object was still in the mutable (pending) state instead of the protected (active/accepted) state. Here, `handleRecvError` mutates/destroys hostmap state for a `hostinfo` without verifying that the request is cryptographically tied to that established session — it only checks a spoofable transport-layer address match.

### Impact Explanation
An attacker with no CA-signed certificate and no valid handshake state can force teardown of another peer's fully established, in-use tunnel purely by crafting a `RecvError` header packet with the correct `RemoteIndex` and a source address that satisfies the address-equality check. This is a remote, unauthenticated denial-of-service against active tunnels: the connection has to fully re-handshake, and if repeated, the tunnel can be kept perpetually torn down (persistent DOS), which is a direct parallel to the "loan can be permanently DOS'd even after being accepted" impact described in the Teller finding. The CHANGELOG itself flags this exact code path as security-sensitive (`listen.send_recv_error` / accept config), confirming it was previously recognized as an information/DOS risk area (#670), yet the core validation remains an address equality check rather than a cryptographic one.

### Likelihood Explanation
Likelihood is elevated by the facts that: (1) the packet type is processed pre-decryption/pre-auth, so no valid certificate or completed handshake is required to send it; (2) `RemoteIndex` values, while random, are only 32 bits and observable by any party that has seen legitimate traffic between the two hosts (e.g., a passive network observer, a malicious former relay, or a NAT-adjacent attacker); and (3) the sole defense is a UDP source-address match, which is inherently spoofable on UDP and additionally satisfiable by any attacker who is on-path or who shares a NAT/relay vantage point with the legitimate remote. The feature is gated by `acceptRecvErrorConfig.ShouldRecvError(addr)`, but that gate is address/allowlist-based configuration, not cryptographic proof of tunnel ownership, and does not require a certificate.

### Recommendation
Do not tear down an established tunnel based solely on an unauthenticated `RecvError` packet whose only validation is UDP source-address equality. At minimum:
- Require that `RecvError` handling only affect hostinfos that are not yet fully established (mirroring the Teller fix of restricting the mutating call to the legitimate lifecycle-owner/state), or
- Bind acceptance of `RecvError` to a value derived from the session's authenticated key material (e.g., a MAC over the index using a key established during the handshake) rather than plaintext index + source-IP match, so an attacker without the negotiated keys cannot forge a valid teardown request.

### Proof of Concept
1. Nodes A and B complete a handshake and have an established tunnel; A has `localIndexId`/`remoteIndexId` visible to any passive observer of the UDP traffic (headers are unencrypted: `header.H.Parse`).
2. An attacker who can observe or infer A's `remoteIndexId` for the tunnel to B (e.g., via traffic observation or by having briefly been on the network path) crafts a raw UDP packet:
   - `header.Encode(..., header.RecvError, 0, remoteIndexId, 0)`
   - sent to A's listening UDP port with a spoofed source address matching B's current `GetRemote()` address (as recorded in A's hostinfo).
3. A's `readOutsidePackets` dispatches this directly to `handleRecvError` (`outside.go:81-84`) without any decryption/cert check.
4. `handleRecvError` finds the hostinfo via `QueryReverseIndex`, sees `hr == addr` (spoofed match), and calls `f.closeTunnel(hostinfo)` plus `f.handshakeManager.DeleteHostInfo(hostinfo)`, killing the active, established tunnel between A and B — accomplished with no certificate, no completed handshake, and no cryptographic proof of session ownership.

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
