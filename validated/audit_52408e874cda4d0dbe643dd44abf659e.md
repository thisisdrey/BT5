### Title
Unauthenticated tunnel teardown via spoofed `RecvError` packets - ([File: outside.go])

### Summary
`Interface.handleRecvError()` tears down an established, mutually-authenticated tunnel in response to a `RecvError` packet that is never cryptographically authenticated. The only two checks performed are (1) a config gate that defaults to accepting from anyone, and (2) a comparison of the spoofable UDP source address against the tunnel's currently known remote address. The 32-bit index needed to target a specific tunnel (`RemoteIndex`) travels in cleartext in every single Nebula packet header, so any on-path/eavesdropping attacker — who holds no CA-signed certificate and is not a party to the tunnel — can learn it and forge a teardown. This mirrors the reported `EscrowVoteManagerV1.poke()` bug class: a state-mutating action reachable by a party with no ownership/authorization over the targeted resource, which forces state changes that disrupt legitimate protocol operation (there, `LockCurrentlyVoting` DoS on `createLock`; here, forced tunnel destruction).

### Finding Description
Nebula's cleartext header format places `RemoteIndex` at a fixed offset in every packet, unauthenticated and unencrypted: [1](#0-0) [2](#0-1) 

`RecvError` is one of the packet types dispatched before any certificate/AEAD verification takes place: [3](#0-2) 

`handleRecvError` processes this unauthenticated packet type and, if it passes a config check plus a *spoofable* UDP source-address comparison, unilaterally tears down the tunnel and deletes host state: [4](#0-3) 

The `ShouldRecvError` config gate defaults to `"always"`, i.e., accepted from any address unless the operator opts into `"private"` or `"never"`: [5](#0-4) 

Because UDP has no return-routability check for this attack (the attacker doesn't need to receive anything back — only to spoof the source `netip.AddrPort` to match the victim's current remote and supply the `RemoteIndex` it observed in a captured/relayed packet header), this is a purely off-band/on-path forgery, not an action requiring possession of a valid certificate or being a genuine peer. This is structurally identical to `EscrowVoteManagerV1.poke(tokenId)` being callable by a caller with no relationship to `tokenId`: a caller with no legitimate stake in a resource can trigger a state transition on it (`closeTunnel`/`DeleteHostInfo` vs. locking the vote state) that disrupts the resource's owner and downstream protocol operations.

### Impact Explanation
An attacker who can observe or infer (a) a victim's current negotiated remote `UDP AddrPort` and (b) the `RemoteIndex` used for an active tunnel (visible in the cleartext header of every packet exchanged on that tunnel) can force `closeTunnel()` plus `handshakeManager.DeleteHostInfo()`, destroying an established, mutually-authenticated tunnel between two legitimate nodes without needing a certificate of their own. Repeated forgery can be used to persistently disrupt connectivity between specific peers, a remote protocol-level denial of service analogous to the reported DoS via `poke()`.

### Likelihood Explanation
Likelihood is limited by (1) needing to observe or guess the 32-bit `RemoteIndex` (feasible for any on-path observer, e.g., a shared network segment, ISP, or a previously-connected relay/lighthouse position, since it is sent in cleartext on every packet of the tunnel) and (2) needing to spoof the victim's current UDP source address, which is generally straightforward on UDP unless egress/anti-spoofing filtering is enforced upstream. Because the default `listen.accept_recv_error` setting is `"always"`, no additional operator hardening is required for the attack to succeed out of the box.

### Recommendation
Do not act on `RecvError` (or any other unauthenticated pre-decryption control message) based solely on address comparison against a guessable/observable index. Require the message to be authenticated within the existing tunnel's cipher/AEAD context (e.g., only honor teardown signals that arrive encrypted under the tunnel's negotiated keys, or require a matching, unpredictable nonce/token established during the handshake) before calling `closeTunnel`/`DeleteHostInfo`. At minimum, default `listen.accept_recv_error` to `"private"`/`"never"` rather than `"always"`, and document the spoofing risk clearly.

### Proof of Concept
1. Establish or observe an active Nebula tunnel between hosts A and B; capture one packet to learn its cleartext `RemoteIndex` (`header/header.go:96,153`) for A's view of B.
2. From a spoofed source (UDP source address forged to A's currently known remote `AddrPort`), send a `header.RecvError` packet to B with that `RemoteIndex`.
3. Observe `handleRecvError` in `outside.go:541-575` pass its address check (since the forged source matches `hostinfo.GetRemote()`) and call `f.closeTunnel(hostinfo)` and `hm.DeleteHostInfo(hostinfo)`, tearing down the legitimate A↔B tunnel without either party having authorized it and without the attacker ever presenting a certificate.

### Citations

**File:** header/header.go (L91-98)
```go
type H struct {
	Version        uint8
	Type           MessageType
	Subtype        MessageSubType
	Reserved       uint16
	RemoteIndex    uint32
	MessageCounter uint64
}
```

**File:** header/header.go (L143-156)
```go
func (h *H) Parse(b []byte) error {
	if len(b) < Len {
		return ErrHeaderTooShort
	}
	// get upper 4 bytes
	h.Version = uint8((b[0] >> 4) & 0x0f)
	// get lower 4 bytes
	h.Type = MessageType(b[0] & 0x0f)
	h.Subtype = MessageSubType(b[1])
	h.Reserved = binary.BigEndian.Uint16(b[2:4])
	h.RemoteIndex = binary.BigEndian.Uint32(b[4:8])
	h.MessageCounter = binary.BigEndian.Uint64(b[8:16])
	return nil
}
```

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
