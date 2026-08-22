### Title
Unauthenticated `RecvError` packets allow spoofed teardown of established, authenticated tunnels - (File: outside.go)

### Summary
The external report's bug class is "a message-processing function fails to validate an input that should be constrained/absent, and that unvalidated input causes an unwanted, attacker-triggerable side effect (loss of state/funds)." In Nebula, the closest reachable analog is `Interface.handleRecvError` in `outside.go`, which acts on a completely unauthenticated, unencrypted `header.RecvError` packet type and uses it to tear down an already-established, cryptographically authenticated tunnel, relying only on a weak, spoofable check.

### Finding Description
`readOutsidePackets` dispatches `header.RecvError` packets before any certificate or AEAD validation is performed — they are explicitly bucketed with the other "Unencrypted packets" types (`Handshake`, `RecvError`): [1](#0-0) 

`handleRecvError` is invoked directly on this raw, unauthenticated packet: [2](#0-1) 

The only "authentication" performed is:
1. A config gate `ShouldRecvError(addr)` (rate/policy based, not cryptographic).
2. A lookup of the hostinfo by `h.RemoteIndex` (a 32-bit value transmitted in cleartext in every packet of the tunnel, and thus observable to any on-path or off-path attacker who can see/guess traffic for that tunnel).
3. A comparison of the current known remote `netip.AddrPort` to the source address of the received UDP datagram (`hr.IsValid() && hr != addr`).

None of these checks involve verifying possession of a CA-signed certificate, a valid handshake, or any cryptographic proof tied to the tunnel's `ConnectionState`. Because UDP is unauthenticated and trivially spoofable at the network layer, and `RemoteIndex` is sent in plaintext on every packet of a live tunnel, an attacker positioned to observe (or guess) a victim's `RemoteIndex`/`UDPAddr` pair can forge a `RecvError` packet with a spoofed source address matching the current remote, causing:
```go
f.closeTunnel(hostinfo)
f.handshakeManager.DeleteHostInfo(hostinfo)
```
This tears down a live, fully-authenticated tunnel with a single unauthenticated packet — remote state poisoning / denial of service that requires no CA-signed certificate at all, unlike the encrypted-packet paths (`Message`, `CloseTunnel`, `Control`, `LightHouse`) which all require successfully decrypting the AEAD payload with `hostinfo.ConnectionState.dKey` first: [3](#0-2) [4](#0-3) 

This is structurally analogous to the ERC20 bridging bug class: a code path that should require a fully-validated/authenticated precondition (there: `msg.value == 0`; here: cryptographic proof of tunnel possession) instead accepts an easily-forgeable/unauthenticated signal and acts on it, producing an unwanted state change (frozen funds there; forcibly destroyed tunnel/session here).

### Impact Explanation
An attacker with no CA-signed certificate, and without ever completing a handshake with the victim, can force-terminate any Nebula tunnel between two legitimate, certificate-holding peers by sending a single spoofed UDP packet, provided they can observe or infer the `RemoteIndex` and current remote `UDPAddr` for that tunnel (both of which travel in plaintext on the wire and are visible to any network-level observer, e.g., a shared network segment, ISP-level MITM, or off-path spoofer able to guess/observe the tuple). This is a remote-state-poisoning/denial-of-service primitive that undermines tunnel availability guarantees without needing to defeat the Noise handshake or certificate verification at all.

### Likelihood Explanation
Likelihood is moderate: it requires knowledge of `RemoteIndex` and the current `UDPAddr` of the target tunnel and the ability to spoof (or be on-path for) the source address, but no cryptographic material. `RemoteIndex` and addresses are visible in every packet of an active tunnel to any passive observer on the path, and UDP source-address spoofing is a well-known, long-standing capability for on-path/off-path attackers on many networks. This mirrors the "mistakenly/maliciously sent value not validated" pattern from the report — medium likelihood, since it depends on network positioning/spoofability rather than defeating the handshake itself.

### Recommendation
Do not allow an unauthenticated packet type to unilaterally tear down an already-established, authenticated tunnel based solely on a plaintext index and an easily spoofed source-address match. At minimum:
- Require some proof tied to the current `ConnectionState` (e.g., an authenticated/keyed value) before acting on a `RecvError`, or
- Treat `RecvError` purely as a hint to *attempt* a fast re-handshake rather than an unconditional `closeTunnel`/`DeleteHostInfo`, and rate-limit/require corroboration (e.g., only honor it after subsequent handshake completion), or
- Restrict/disable acceptance of `RecvError` by default (`accept_recv_error`) unless the operator has a way to add source authentication.

### Proof of Concept
1. Passively observe or infer a victim tunnel's `RemoteIndex` (visible in every packet header of the live tunnel) and the current `UDPAddr` peers are using.
2. Craft a `header.RecvError` packet (`header.Encode(..., header.RecvError, 0, RemoteIndex, 0)`), no encryption/signature needed, matching the format built in `sendRecvError`: [5](#0-4) 
3. Send it over UDP to the victim with a spoofed source address equal to the victim's current known remote (or be on-path so the natural source address matches).
4. `handleRecvError` finds the hostinfo by `RemoteIndex`, sees `hr == addr`, and calls `f.closeTunnel(hostinfo)` plus `DeleteHostInfo`, destroying the tunnel state without ever presenting a CA-signed certificate or completing a handshake.

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

**File:** outside.go (L105-132)
```go
	if len(packet) < header.Len+hostinfo.ConnectionState.dKey.Overhead() {
		f.messageMetrics.RxInvalid(1)
		if f.l.Enabled(context.Background(), slog.LevelDebug) {
			f.l.Debug("packet too small", "from", via, "length", len(packet))
		}
		return
	}

	// All remaining packets are encrypted
	if isMessageRelay {
		// Relay packets are special, this branch should always early-return
		if err = hostinfo.ConnectionState.VerifyRelay(f.l, h.MessageCounter, packet, nb); err != nil {
			if f.l.Enabled(context.Background(), slog.LevelDebug) {
				hostinfo.logger(f.l).Debug("Failed to verify relay packet", "error", err, "from", via, "header", h)
			}
			return
		}
		f.handleOutsideRelayPacket(hostinfo, via, out, packet, h, fwPacket, lhf, nb, q, localCache)
		return
	}

	out, err = hostinfo.ConnectionState.Decrypt(f.l, h.MessageCounter, out, packet, nb)
	if err != nil {
		if f.l.Enabled(context.Background(), slog.LevelDebug) {
			hostinfo.logger(f.l).Debug("Failed to decrypt packet", "error", err, "from", via, "header", h)
		}
		return
	}
```

**File:** outside.go (L138-173)
```go
	switch h.Type {
	case header.Message:
		switch h.Subtype {
		case header.MessageNone:
			f.handleOutsideMessagePacket(hostinfo, out, packet, fwPacket, nb, q, localCache)
		default:
			hostinfo.logger(f.l).Error("IsValidSubType was true, but unexpected message subtype seen", "from", via, "header", h)
			return
		}

	case header.LightHouse:
		//TODO: assert via is not relayed
		lhf.HandleRequest(via.UdpAddr, hostinfo.vpnAddrs, out, f)

	case header.Test:
		switch h.Subtype {
		case header.TestReply:
			// No-op, useful for the Roaming and connectionManager side-effects above
		case header.TestRequest:
			//recycle the input packet ciphertext as our output buffer
			f.send(header.Test, header.TestReply, hostinfo.ConnectionState, hostinfo, out, nb, packet)
		default:
			hostinfo.logger(f.l).Error("IsValidSubType was true, but unexpected test subtype seen", "from", via, "header", h)
			return
		}

	case header.CloseTunnel:
		hostinfo.logger(f.l).Info("Close tunnel received, tearing down.", "from", via)
		f.closeTunnel(hostinfo)

	case header.Control:
		f.relayManager.HandleControlMsg(hostinfo, out, f)

	default:
		hostinfo.logger(f.l).Error("IsValidSubType was true, but unexpected message type seen", "from", via, "header", h)
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
