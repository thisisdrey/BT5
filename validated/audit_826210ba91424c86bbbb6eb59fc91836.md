### Title
Unauthenticated `RecvError` packet allows a certificate-less attacker to tear down any established tunnel - (File: outside.go)

### Summary
Nebula processes `header.RecvError` packets before any cryptographic authentication is performed on the packet. `Interface.handleRecvError` tears down a fully-authenticated, already-established tunnel based only on (1) a coarse `accept_recv_error` policy check and (2) a comparison of the packet's *source UDP address* against the hostinfo's currently known remote address — neither of which requires possession of a certificate, private key, or any cryptographic proof tied to the tunnel. An attacker who can spoof the victim peer's UDP source address (or who is on-path) and who knows/observes the 32-bit `RemoteIndex` (which is transmitted in cleartext in every packet header) can forge a single unauthenticated packet that strips away a tunnel that took a full mutually-authenticated Noise handshake to establish.

### Finding Description
Inbound UDP packets are dispatched by type in `readOutsidePackets` before hostinfo/ConnectionState decryption is attempted: [1](#0-0) 

`header.RecvError` is handled immediately, with no AEAD/Noise verification of the packet contents: [2](#0-1) 

`handleRecvError` only performs:
1. A config-driven policy gate `f.acceptRecvErrorConfig.ShouldRecvError(addr)`, which by default (`recvErrorAlways`) simply returns `true` for any address: [3](#0-2) 
2. A plaintext comparison of the source `addr` against `hostinfo.GetRemote()`. If they match, the code unconditionally calls `f.closeTunnel(hostinfo)` and also deletes the pending handshake state — with zero cryptographic proof that the sender actually is that peer or holds any valid certificate.

Once matched, the call chain destroys the already-established, fully-authenticated secure tunnel: [4](#0-3) [5](#0-4) 

This is precisely analogous to the "Artist can set GoldenEggFee to zero, rugging winner" bug class: a protection or established state that was earned/verified through a proper authenticated process (a completed CA-signed handshake, i.e., the "winnings") can be unilaterally stripped at any time by an entity that never had to prove it held the relevant credential (an attacker with no certificate at all, only the ability to spoof a source address and observe a cleartext index). Just as the external report's fix was to gate the setter behind a proper phase/authorization check (`onlyBeforeSAMPhase`), Nebula's `RecvError` path lacks any binding to a cryptographic/authenticated proof of the sender's identity before it is allowed to invalidate protected connection state.

### Impact Explanation
An unauthenticated attacker (no CA-signed certificate, no valid keys) can force teardown of any live Nebula tunnel between two legitimate, mutually-authenticated peers by:
- Spoofing the UDP source address to equal the victim's peer's known remote endpoint (feasible for on-path attackers, and for off-path attackers on networks that don't enforce BCP38/source-address validation), and
- Supplying the target's `RemoteIndex`, which travels in cleartext in every packet's header and is therefore trivially observable by anyone who can see any traffic to/from the victim.

This is remote state poisoning of the tunnel/connection state without any authentication — the exact class of "stripping an already-verified/established guarantee" called out in the external report, just applied to a persistent, repeatable tunnel-teardown DoS rather than a one-time prize.

### Likelihood Explanation
The check only requires knowledge of the victim's currently-active remote `netip.AddrPort` and `RemoteIndex`, both learnable via passive observation or via typical lighthouse/UDP address disclosure inherent to Nebula's design, combined with source-address spoofing. No cryptographic material is needed. Nebula's own changelog documents `recv_error` handling being a known source of concern (`listen.send_recv_error`, `pki.accept_recv_error`-style controls, and a 2025 change to "Disable sending recv_error messages when a packet is received outside the allowable counter window"), indicating maintainers are aware this control-plane message is sensitive, but the accept path still relies solely on unauthenticated `RemoteIndex` + address matching rather than cryptographic verification.

### Recommendation
Do not allow an unauthenticated `RecvError` packet to unilaterally destroy hostinfo/connection state. At minimum:
- Require some proof tied to the current session (e.g., only honor `RecvError` if it can be correlated with a recently sent packet whose ciphertext/counter the attacker could not have known), or
- Rate-limit / require repeated confirmation via the authenticated path (e.g., verified TestRequest/TestReply round trip) before tearing down an established tunnel, rather than immediately closing on a single plaintext, addr-matched `RecvError`.

### Proof of Concept
1. Establish a normal authenticated tunnel between `me` and `them`.
2. Attacker observes any packet on the wire to learn `them`'s `RemoteIndex` (sent in cleartext header) and `them`'s current UDP remote address (also visible in header/lighthouse traffic).
3. Attacker crafts a raw UDP packet: `header.H{Type: header.RecvError, RemoteIndex: <victim's index>}`, sets the spoofed source address to the victim peer's known remote endpoint, and sends it to `me`.
4. `readOutsidePackets` routes it to `handleRecvError` without any decryption/verification ( [6](#0-5) ), the address check passes because the source was spoofed to match, and `me`'s fully-authenticated tunnel to `them` is torn down ( [7](#0-6) ), even though the attacker never held a certificate for either endpoint.

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

**File:** outside.go (L250-257)
```go
// closeTunnel closes a tunnel locally, it does not send a closeTunnel packet to the remote
func (f *Interface) closeTunnel(hostInfo *HostInfo) {
	final := f.hostMap.DeleteHostInfo(hostInfo)
	if final {
		// We no longer have any tunnels with this vpn addr, clear learned lighthouse state to lower memory usage
		f.lightHouse.DeleteVpnAddrs(hostInfo.vpnAddrs)
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

**File:** interface.go (L124-143)
```go
type recvErrorConfig uint8

const (
	recvErrorAlways recvErrorConfig = iota
	recvErrorNever
	recvErrorPrivate
)

func (s recvErrorConfig) ShouldRecvError(endpoint netip.AddrPort) bool {
	switch s {
	case recvErrorPrivate:
		return endpoint.Addr().IsPrivate()
	case recvErrorAlways:
		return true
	case recvErrorNever:
		return false
	default:
		panic(fmt.Errorf("invalid recvErrorConfig value: %d", s))
	}
}
```
