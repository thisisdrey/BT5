### Title
Unauthenticated `RecvError` packets let a network attacker repeatedly veto/tear down any tunnel, permanently blocking re-establishment — ([File: outside.go])

### Summary
Nebula's `header.RecvError` control message is processed before any certificate or MAC authentication and, once the sender's claimed UDP source matches the target hostinfo's currently-known remote address, unconditionally tears down the tunnel and purges the pending-handshake state. Because the check is based only on the spoofable UDP source address (not on any cryptographic proof of identity), an attacker who does not hold a CA-signed certificate can repeatedly forge `RecvError` packets to keep vetoing a victim's tunnel, exactly mirroring the "canceller can veto/cancel indefinitely" pattern from the external report, except here the object being permanently blocked is tunnel establishment/traffic flow rather than a timelocked governance action.

### Finding Description
`readOutsidePackets` dispatches `header.RecvError` to `f.handleRecvError` prior to any decryption or handshake completion check [1](#0-0) . `handleRecvError` only verifies (a) local policy (`acceptRecvErrorConfig`) and (b) that the packet's source address equals the hostinfo's currently recorded remote endpoint — a value derived purely from previously observed UDP source addresses, not from cryptographic authentication: [2](#0-1) 

If the addresses match, the code unconditionally calls `f.closeTunnel(hostinfo)` and `f.handshakeManager.DeleteHostInfo(hostinfo)` — tearing down the live tunnel and also purging any in-flight pending handshake for the same peer, so a follow-up legitimate handshake attempt is discarded too.

This is analogous to the report's bug class: a party without full trust (here, an attacker who never presented a CA-signed certificate, since `RecvError` is handled entirely outside the encrypted/authenticated handshake channel) can repeatedly invoke a "cancel" primitive (`RecvError` → teardown) against a target that a legitimate admin/peer cannot cheaply override, since the victim has no way to distinguish forged `RecvError` packets from genuine ones sent by its real peer. As long as the attacker can spoof the victim's real communicating peer's UDP source address/port (trivial on many networks/ISPs that don't filter source IP, or via any on-path position) and knows/guesses the `RemoteIndex` carried in the header, they can indefinitely veto tunnel formation — a persistent "lock the safe" style denial of the overlay tunnel.

### Impact Explanation
An attacker with no valid Nebula certificate can force repeated teardown of any target's tunnel(s) to a specific peer merely by spoofing that peer's known UDP endpoint in unauthenticated `RecvError` packets. Because `DeleteHostInfo` also clears pending handshake state, the victim's attempt to re-establish is also discarded, and repeated injection produces a sustained denial of service that a legitimate host cannot self-heal from as long as the attack continues — the overlay-network equivalent of the Safe being permanently unable to execute operations due to a hostile, unauthenticated actor able to veto recovery attempts.

### Likelihood Explanation
Exploitability depends on the attacker being able to spoof UDP packets appearing to originate from the victim's real peer address and to supply a `RemoteIndex` matching an active/pending hostinfo. `RemoteIndex` values are attacker-unpredictable 32-bit random values in the common case, but an attacker positioned on-path (e.g., shared/compromised network segment, malicious ISP hop, or simple UDP source-spoofing on networks without egress filtering) can trivially observe both the endpoint and index from the legitimate traffic and replay a crafted, unauthenticated `RecvError`. This is default-enabled behavior (`listen.accept_recv_error` defaults to `always`) [3](#0-2) , so no special victim configuration is required.

### Recommendation
Do not act on `RecvError` packets purely on the basis of the spoofable source address. Options include: requiring `RecvError` handling only from an endpoint that has been roamed-to/verified via authenticated traffic, rate-limiting/back-off before honoring repeated `RecvError` for the same hostinfo, or gating tunnel teardown behind additional corroborating signal (e.g., only close after failing to receive a valid encrypted response within a bounded window) rather than immediate unconditional teardown on receipt of an unauthenticated control packet.

### Proof of Concept
1. Passively observe (or otherwise learn) the UDP `AddrPort` and `RemoteIndex` of an established/pending tunnel between victim V and legitimate peer P (e.g., via a shared network segment).
2. Craft a `header.RecvError` packet with `RemoteIndex` set to V's `localIndexId` for that tunnel, per the wire format used in `sendRecvError` [4](#0-3) .
3. Send this packet to V from a spoofed source address equal to P's known UDP endpoint (no valid nebula certificate is presented or required, since `RecvError` bypasses the handshake/authentication path entirely).
4. Observe `handleRecvError` accept the packet because `hr == addr` and call `closeTunnel` + `handshakeManager.DeleteHostInfo`, tearing down V's tunnel and pending handshake state [5](#0-4) .
5. Repeat continuously to prevent V and P from ever maintaining or re-establishing a tunnel.

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

**File:** interface.go (L459-480)
```go
func (f *Interface) reloadAcceptRecvError(c *config.C) {
	if c.InitialLoad() || c.HasChanged("listen.accept_recv_error") {
		stringValue := c.GetString("listen.accept_recv_error", "always")

		switch stringValue {
		case "always":
			f.acceptRecvErrorConfig = recvErrorAlways
		case "never":
			f.acceptRecvErrorConfig = recvErrorNever
		case "private":
			f.acceptRecvErrorConfig = recvErrorPrivate
		default:
			if c.GetBool("listen.accept_recv_error", true) {
				f.acceptRecvErrorConfig = recvErrorAlways
			} else {
				f.acceptRecvErrorConfig = recvErrorNever
			}
		}

		f.l.Info("Loaded accept_recv_error config", "acceptRecvError", f.acceptRecvErrorConfig.String())
	}
}
```
