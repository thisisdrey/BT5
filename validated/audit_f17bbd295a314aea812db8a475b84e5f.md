Found it: `handleRecvError` in `outside.go` accepts an unauthenticated, unencrypted `RecvError` packet type and closes the tunnel based solely on the sender's UDP source address matching the hostinfo's current remote address — with no cryptographic authentication of the message itself.

### Title
Unauthenticated `RecvError` packet allows any attacker to force-close an established tunnel - (File: outside.go)

### Summary
The `header.RecvError` message type is processed in the "Unencrypted packets" branch of `readOutsidePackets`, before any decryption or key-based authentication occurs. `handleRecvError` tears down a live, fully-established tunnel using only a spoofable source-IP/port comparison, analogous to the MapleLoan `closeLoan()` bug where a privileged teardown action lacked a sender-authorization check.

### Finding Description
`readOutsidePackets` dispatches `header.RecvError` directly to `f.handleRecvError(via.UdpAddr, h)` in the unencrypted-packet switch, prior to any hostinfo lookup, decryption, or session-key verification: [1](#0-0) 

`handleRecvError` then looks up the hostinfo purely by `h.RemoteIndex` (a value taken directly from the unauthenticated packet header) and, if the packet's source UDP address matches the hostinfo's currently known remote address, immediately closes the tunnel and deletes the pending handshake state: [2](#0-1) 

The only "authorization" check is `hr.IsValid() && hr != addr` — an IP/port comparison on a raw UDP header field, which is trivially spoofable by any off-path or on-path attacker who can guess/observe the remote UDP endpoint and the target's `RemoteIndex` (which is sent in the clear on every wire packet, including the initial handshake). There is no cryptographic proof that the sender possesses the session key or certificate for that tunnel — no CA-signed certificate is required. This mirrors the `closeLoan()` finding: a state-destroying operation is reachable by anyone who can construct the right packet shape, without proving they are the authorized counterparty.

### Impact Explanation
An attacker with no valid certificate, who can merely observe or guess a victim's UDP source address/port and the wire-visible `RemoteIndex`, can forge a single unencrypted `RecvError` packet to force `closeTunnel` + `DeleteHostInfo`, tearing down an active, authenticated VPN tunnel. This is a remote state-poisoning / denial-of-service primitive: repeated forged `RecvError` packets can prevent two legitimate peers from maintaining a stable tunnel, forcing continual re-handshakes (griefing), directly paralleling the "borrower and lender must abandon the contract and redo everything" impact in the original finding.

### Likelihood Explanation
`RemoteIndex` values are transmitted unencrypted in every packet header (handshake and data), and UDP source `addr:port` is visible to any on-path observer and easily spoofed by an off-path attacker over UDP (no TCP handshake needed). `acceptRecvErrorConfig.ShouldRecvError` is a configurable rate/scope gate, not a cryptographic authentication check, so the attack surface exists whenever recv_error acceptance is enabled. The CHANGELOG shows this exact class of message was already been hardened in one direction ("Disable sending recv_error messages when a packet is received outside the allowable counter window" for the *sender* side) — confirming this message type is recognized as a known weak/unauthenticated-teardown signal, but the *receiver*-side authorization gap (matching only on spoofable source address) remains: [3](#0-2) 

### Recommendation
Do not act on `RecvError` based solely on comparing the UDP source address. Require that tunnel teardown triggered by `RecvError` only occur when corroborated by a legitimately authenticated signal (e.g., only act on `RecvError` for indices tied to handshakes not yet cryptographically completed, or require the message to be bound to a recent, verifiably-sent packet/nonce), and treat address-only matches as advisory rather than sufficient authorization to call `closeTunnel`/`DeleteHostInfo`.

### Proof of Concept
1. Attacker observes (or brute-forces) a live tunnel's wire-visible `RemoteIndex` value, e.g. from any packet exchanged between victim peers A and B (indices are sent in cleartext in the header of every packet, including handshake stage 1/2).
2. Attacker crafts a bare `header.RecvError` packet (`header.Encode(..., header.RecvError, 0, index, 0)`) with a spoofed UDP source address equal to peer B's known `RemoteAddr`, and sends it to peer A.
3. `readOutsidePackets` routes it straight to `handleRecvError` without decryption: [4](#0-3) 
4. `handleRecvError` finds the hostinfo via `QueryReverseIndex(h.RemoteIndex)`, sees `hr == addr` (spoofed), and calls `f.closeTunnel(hostinfo)` plus `f.handshakeManager.DeleteHostInfo(hostinfo)`, destroying the tunnel state on peer A with no proof the attacker holds any key or certificate: [5](#0-4)

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

**File:** CHANGELOG.md (L188-191)
```markdown
### Changed

- Disable sending `recv_error` messages when a packet is received outside the allowable counter window. (#1459)
- Improve error messages and remove some unnecessary fatal conditions in the Windows and generic udp listener. (#1453)
```
