Confirmed: the header (including `RemoteIndex`) is sent entirely in cleartext for every packet type, including encrypted `Message` packets, since it precedes the encrypted payload.### Title
Unauthenticated `RecvError` packets allow a certless remote attacker to forcibly tear down (halt) established tunnels - ([File: outside.go])

### Summary
The external report's bug class is a privileged-action guarded only by an insufficient access-control check (`onlyStrategist` instead of `onlyGovernance`), letting a lesser-privileged actor unilaterally halt protocol state. The analogous flaw in this codebase is `Interface.handleRecvError`, which tears down an active, authenticated tunnel (`f.closeTunnel(hostinfo)`) based solely on an unauthenticated, cleartext `RecvError` control packet whose only "authorization" check is a spoofable UDP source-address comparison — not a cryptographic proof of possession of the tunnel's session keys or a valid certificate.

### Finding Description
`readOutsidePackets` dispatches `header.RecvError` packets directly to `f.handleRecvError` before any handshake, certificate, or encryption check is performed: [1](#0-0) 

`handleRecvError` then looks up the target tunnel purely by the cleartext `RemoteIndex` field from the header, and the only gate against forgery is a plain equality check between the UDP source address of the packet and the `HostInfo`'s currently-known remote address: [2](#0-1) 

The `RemoteIndex` and all other header fields are transmitted unencrypted for every single packet on the wire (it precedes/gates the encrypted payload and is parsed before any decryption occurs): [3](#0-2) 

Because UDP source addresses are trivially spoofable (no return-routability or cryptographic proof is required for `RecvError`), and `RemoteIndex` is visible in cleartext to any on-path observer or relay, an attacker with no CA-signed certificate — i.e., someone who never completed the Noise/IX handshake and holds no valid `cert.Certificate` — can craft a `RecvError` packet with a spoofed source address and an observed `RemoteIndex` to cause `f.closeTunnel(hostinfo)` plus `f.handshakeManager.DeleteHostInfo(hostinfo)` to fire on a victim's legitimate, already-authenticated tunnel. This is conceptually identical to the report's core defect: a state-mutating "halt" action reachable by an actor who should not be trusted to perform it, gated by a weak/wrong authorization check instead of genuine credential verification.

### Impact Explanation
Unauthenticated teardown of tunnels is a direct denial-of-service against the mesh: repeated forged `RecvError` packets can perpetually tear down tunnels as soon as they are (re)established, preventing any durable connectivity between two nodes — a remote, certificate-less actor forcing a persistent "halt" of the overlay link, analogous to the report's permanent-shutdown impact. This satisfies the "remote crash/state poisoning" impact bar (forced connection state destruction is remote state poisoning of the tunnel state machine).

### Likelihood Explanation
Likelihood is bounded by two attacker capabilities that must both be met: (1) knowledge of a victim's live `RemoteIndex` (visible in cleartext in any packet header the attacker can observe, e.g. via network position, or a relay role) and (2) the ability to spoof the UDP source address of the peer, or to be positioned such that address spoofing/source injection is possible (common on many networks lacking egress/ingress filtering). Maintainers appear partially aware of `RecvError` abuse risk, since `send_recv_error`/`accept_recv_error` config knobs exist to gate this behavior, but the default posture (`recvErrorAlways`) still permits the flow described, and the spoofable source-check is the only protection in the accept path.

### Recommendation
Do not allow a plaintext, unauthenticated control message to mutate live tunnel state. Require `RecvError` handling to be authenticated — e.g., only accept it over an already-authenticated channel (encrypt/HMAC it with the tunnel's derived key, or require it to arrive alongside a valid AEAD-verified nonce/counter under the existing session), rather than relying on comparing the UDP source `netip.AddrPort` to `hostinfo.GetRemote()`. At minimum, require it to be conditioned on rate limiting plus the existing `accept_recv_error` scope narrowed by default, and treat address-based checks as insufficient for a destructive action.

### Proof of Concept
1. Attacker observes (or is relayed) traffic between node A and node B, learning B's `RemoteIndex` for the tunnel to A (visible in the cleartext header of every packet per `header.H.Parse`). [4](#0-3) 
2. Attacker crafts a UDP packet with `header.Encode(..., header.RecvError, 0, capturedRemoteIndex, 0)` and spoofs the source address to match A's known UDP endpoint as seen by B. [5](#0-4) 
3. Sends it to B. `readOutsidePackets` routes it straight to `handleRecvError` without any certificate/handshake requirement. [6](#0-5) 
4. `handleRecvError` finds the hostinfo via `QueryReverseIndex`, sees the spoofed address matches `hostinfo.GetRemote()`, and calls `f.closeTunnel(hostinfo)` — the tunnel is torn down with no cryptographic authentication of the requester. [7](#0-6)

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

**File:** header/header.go (L91-156)
```go
type H struct {
	Version        uint8
	Type           MessageType
	Subtype        MessageSubType
	Reserved       uint16
	RemoteIndex    uint32
	MessageCounter uint64
}

// Encode uses the provided byte array to encode the provided header values into.
// Byte array must be capped higher than HeaderLen or this will panic
func Encode(b []byte, v uint8, t MessageType, st MessageSubType, ri uint32, c uint64) []byte {
	b = b[:Len]
	b[0] = v<<4 | byte(t&0x0f)
	b[1] = byte(st)
	binary.BigEndian.PutUint16(b[2:4], 0)
	binary.BigEndian.PutUint32(b[4:8], ri)
	binary.BigEndian.PutUint64(b[8:16], c)
	return b
}

// String creates a readable string representation of a header
func (h *H) String() string {
	if h == nil {
		return "<nil>"
	}
	return fmt.Sprintf("ver=%d type=%s subtype=%s reserved=%#x remoteindex=%v messagecounter=%v",
		h.Version, h.TypeName(), h.SubTypeName(), h.Reserved, h.RemoteIndex, h.MessageCounter)
}

// MarshalJSON creates a json string representation of a header
func (h *H) MarshalJSON() ([]byte, error) {
	return json.Marshal(m{
		"version":        h.Version,
		"type":           h.TypeName(),
		"subType":        h.SubTypeName(),
		"reserved":       h.Reserved,
		"remoteIndex":    h.RemoteIndex,
		"messageCounter": h.MessageCounter,
	})
}

// Encode turns header into bytes
func (h *H) Encode(b []byte) ([]byte, error) {
	if h == nil {
		return nil, errors.New("nil header")
	}

	return Encode(b, h.Version, h.Type, h.Subtype, h.RemoteIndex, h.MessageCounter), nil
}

// Parse is a helper function to parses given bytes into new Header struct
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
