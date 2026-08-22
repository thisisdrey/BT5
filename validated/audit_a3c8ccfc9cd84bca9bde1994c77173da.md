Confirmed: `RemoteIndex` is a plaintext header field on every packet (`header/header.go` lines 96, 107, 153), so any on-path observer learns it without needing a certificate. This confirms the RecvError-based griefing analog.### Title
Unauthenticated `RecvError` teardown allows griefing of established tunnels via spoofed plaintext-index packets - ([File: outside.go])

### Summary
Nebula's `RecvError` message type is processed entirely outside the AEAD/handshake-authenticated path. It is dispatched straight from `readOutsidePackets` before any decryption or Noise verification, and the only check performed is a loose comparison against the hostinfo's currently known remote address. Because the `RemoteIndex` field used to find the target hostinfo is sent in cleartext in the header of *every* nebula packet (handshake, data, test, etc.), a network-position attacker who can merely observe traffic between two peers (no CA-signed certificate required) can learn the index, spoof the victim's own remote UDP source address, and forge a `RecvError` packet that tears down the live tunnel. This mirrors the Lombard bug class: an unauthenticated/unconfirmed input is trusted to trigger an irreversible state transition (tunnel teardown) that grieves the legitimate parties, forcing them to constantly re-handshake or lose connectivity — a repeatable denial-of-service against a class of bug ("forged and unconfirmed" data driving state changes without full protocol confirmation).

### Finding Description
In `outside.go`, `readOutsidePackets` parses the header and, for `header.RecvError`, calls `f.handleRecvError` immediately — well before any AEAD decrypt/verify step that other message types (`Message`, `Test`, `LightHouse`, `CloseTunnel`) go through: [1](#0-0) 

`handleRecvError` looks up the hostinfo purely by the plaintext `RemoteIndex` field via `QueryReverseIndex`, and the only anti-spoofing check is comparing the claimed source address to the hostinfo's last known remote: [2](#0-1) 

The `RemoteIndex` field is not encrypted or authenticated — it is a plaintext field in every packet header, as defined by the wire format and parse/encode functions: [3](#0-2) [4](#0-3) 

Consequently, an attacker who can observe even a single packet exchanged between the two real peers (e.g., anyone on a shared network segment, an upstream router, a Wi-Fi eavesdropper, or an ISP) learns the exact `RemoteIndex` needed to target a specific tunnel, without ever needing a nebula certificate. Combined with straightforward UDP source-address spoofing (routine for on-path/off-path attackers on many networks), the attacker can synthesize a `RecvError` packet that:
1. Passes the address check in `handleRecvError` (spoofed source == hostinfo's current remote).
2. Causes `f.closeTunnel(hostinfo)` and `f.handshakeManager.DeleteHostInfo(hostinfo)` to run — unconditionally tearing down the tunnel.

This is analogous to the reported Lombard issue: a message that has not gone through the full trust/confirmation process (there, notary quorum; here, cryptographic packet authentication) is nonetheless used to force an authoritative, disruptive state change (there, marking unstake "paid"; here, destroying an active tunnel), griefing the legitimate parties.

### Impact Explanation
A successful forged `RecvError` immediately destroys an active, authenticated tunnel between two legitimate peers. Because the attacker only needs to observe traffic once to learn the `RemoteIndex` (it never changes for the life of the tunnel) and can then repeat the forged packet at will, this enables a persistent, repeatable denial-of-service against specific tunnels — forcing continuous re-handshakes and connectivity loss, which is a "remote crash/DoS impact" and "remote state poisoning" style outcome. Unlike a normal transient packet-loss condition, this is an intentional, attacker-controlled disruption that a legitimate protocol confirmation step (AEAD authentication) should have prevented.

### Likelihood Explanation
Likelihood is contingent on two capabilities that are individually well within reach of a non-certificate-holding network attacker: (1) observing at least one packet on the path between the two peers to read the plaintext `RemoteIndex`, and (2) UDP source-address spoofing to match the expected remote endpoint. Both are commonly available to attackers positioned on shared/insecure network segments (public Wi-Fi, compromised routers, transit ISPs without BCP38 filtering). The changelog shows this general area (spoofed `recv_error` handling, `1.9.7` and `#482`) has already been a source of back-and-forth security fixes, indicating the developers are aware this attack surface is sensitive but the current mitigation (address-match check only) does not fully close it since UDP source spoofing defeats an address-only check.

### Recommendation
Do not allow an unauthenticated packet type to unilaterally tear down an established, cryptographically-verified tunnel. Options:
- Require `RecvError` handling to only take effect after some form of cryptographic confirmation (e.g., signed/HMAC'd with a value derived from `ConnectionState`, or require correlating multiple independent signals before tearing down).
- Alternatively, downgrade the effect of an unauthenticated `RecvError` from an immediate `closeTunnel` + `DeleteHostInfo` to a soft signal (e.g., trigger a re-verification test packet through the already-authenticated channel) rather than an immediate teardown.
- Continue to honor the existing `listen.accept_recv_error` config (`always`/`never`/`private`) but document/default it away from `always` for untrusted network environments, and consider rate-limiting/logging repeated `RecvError` events per remote index as a spoofing indicator.

### Proof of Concept
1. Attacker observes any single packet (of any type) exchanged between "me" and "them" and reads the plaintext header to obtain `RemoteIndex` (`h.RemoteIndex`) as well as the current UDP addr/port pair in use — as demonstrated by the existing `e2e/tunnels_test.go` `TestCloseTunnelAuthenticated` test, which shows a bogus packet can be crafted this way for `CloseTunnel`; the same header-based targeting equally applies to `RecvError`: [5](#0-4) 
2. Attacker crafts a `header.RecvError` packet with `RemoteIndex` set to the observed index, and spoofs the UDP source address to match the victim's real remote address (`hr == addr` check in `handleRecvError`).
3. Attacker sends this forged packet to the victim.
4. `readOutsidePackets` dispatches it directly to `handleRecvError` without any decryption/authentication: [6](#0-5) 
5. `handleRecvError`'s address check passes because the source was spoofed to match, and the victim's tunnel is torn down (`f.closeTunnel(hostinfo)` and pending-hostmap deletion): [7](#0-6) 
6. Repeating this at will produces a persistent denial-of-service on the targeted tunnel.

**Note on confidence/limitations:** I could not locate the exact implementation of `ShouldRecvError`/the `recvErrorAlways`/`recvErrorPrivate`/`recvErrorNever` enum bodies in the indexed content (only their usage sites in `interface.go` and `outside.go` were retrievable), so I cannot fully confirm whether the `private` mode provides any additional network-origin filtering that could partially mitigate this issue in some configurations. If precise verification of that logic is required, a full Devin session with complete file access to `interface.go` would be needed to inspect the `recvErrorAlways`/`recvErrorPrivate` type definitions.

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

**File:** header/header.go (L10-20)
```go
//Version 1 header:
// 0                                                                       31
// |-----------------------------------------------------------------------|
// | Version (uint4) | Type (uint4) |  Subtype (uint8) | Reserved (uint16) | 32
// |-----------------------------------------------------------------------|
// |                        Remote index (uint32)                          | 64
// |-----------------------------------------------------------------------|
// |                           Message counter                             | 96
// |                               (uint64)                                | 128
// |-----------------------------------------------------------------------|
// |                               payload...                              |
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

**File:** e2e/tunnels_test.go (L528-547)
```go
	buf := make([]byte, 1024)
	hdr := header.H{
		Version:        1,
		Type:           header.CloseTunnel,
		Subtype:        0,
		Reserved:       0,
		RemoteIndex:    hi.RemoteIndex,
		MessageCounter: 5,
	}
	out, err := hdr.Encode(buf)
	if err != nil {
		t.Fatal(err)
	}

	pkt := &udp.Packet{
		To:   hi.CurrentRemote,
		From: myHi.CurrentRemote,
		Data: out,
	}
	r.InjectUDPPacket(myControl, theirControl, pkt)
```
