### Title
Unauthenticated `recv_error` packets let a spoofing attacker force tunnel teardown - (File: `outside.go`)

### Summary
`Interface.handleRecvError` in `outside.go` tears down an active tunnel based purely on two attacker-controlled, unauthenticated inputs pulled straight off the wire: the cleartext `RemoteIndex` field of the Nebula header, and the UDP source address of the packet. Neither of these is protected by any AEAD/MAC check, mirroring the root cause of the referenced report: an untrusted, externally-injectable raw quantity is fed directly into a state-changing operation (there: `currencyToken.balanceOf`, here: `header.RemoteIndex` + spoofed UDP source), causing a legitimate protocol operation (there: liquidity migration, here: tunnel establishment/maintenance) to fail/DoS.

### Finding Description
`readOutsidePackets` dispatches `header.RecvError` packets to `handleRecvError` before any decryption or authentication occurs: [1](#0-0) 

`handleRecvError` then:
1. Looks up a hostinfo purely by the cleartext, attacker-supplied `h.RemoteIndex` via `QueryReverseIndex`.
2. Compares the packet's UDP source address against the hostinfo's currently known remote address.
3. If they match (or if the hostinfo has no remote set yet), it calls `f.closeTunnel(hostinfo)` and deletes it from the pending handshake manager — with no cryptographic authentication whatsoever: [2](#0-1) 

The only "protection" is `hr != addr` — a check that source address matches the value Nebula itself observed for that peer. `RemoteIndex` is a 32-bit value transmitted in the clear on every packet (both directions) for the life of the tunnel, so any attacker who can observe traffic (or simply guess across the 32-bit space) and can spoof the legitimate peer's UDP source address (a classic unauthenticated network capability, no CA-signed certificate required) can synthesize a `recv_error` packet that satisfies both checks and forces `closeTunnel`, exactly analogous to how the referenced bug let an attacker manipulate an unauthenticated raw quantity (`balanceOf`) to force a downstream revert.

The project's own changelog documents past hardening in this exact area (restricting when `recv_error` is honored/sent, e.g. `#1459`, `#670`, `#482`) confirming this is a recognized, still-present trust boundary rather than a fully mitigated path — `acceptRecvErrorConfig`/`sendRecvErrorConfig` reduce but do not eliminate the exposure for any configuration where recv_error acceptance is enabled (e.g. `recvErrorAlways`, or `recvErrorPrivate` when the spoofed source is within a private range the attacker can reach). [3](#0-2) 

### Impact Explanation
An unauthenticated, off-path (or on-path) attacker who can spoof UDP source addresses can force teardown of an established, legitimate tunnel between two Nebula nodes without holding any valid certificate or key material — a direct denial-of-service on the overlay network's core connectivity, analogous to the "protocol disruption" impact called out in the referenced report (there: liquidity migration DoS; here: tunnel/connectivity DoS). Repeated spoofed `recv_error` packets can be used to continuously prevent a tunnel from staying up.

### Likelihood Explanation
Likelihood depends on: (a) the deployment's `listen.accept_recv_error` setting (`recvErrorAlways` is fully exposed; `recvErrorPrivate` restricts to private source ranges; `recvErrorNever` closes this path entirely), and (b) the attacker's ability to spoof UDP source IPs and/or learn/guess the 32-bit `RemoteIndex`, which is sent unencrypted on every packet and thus trivially observable by anyone who can capture traffic between the peers (e.g., a compromised router, ISP-level observer, or same-LAN attacker). No CA-signed certificate is required at any point in this attack.

### Recommendation
Do not act on `recv_error` (or any other unauthenticated control message) without cryptographic proof tying it to the session — e.g. require the message to be authenticated with the session's AEAD key/counter (as data-plane and relay traffic already is via `ConnectionState.Decrypt`/`VerifyRelay`), or drop `recv_error` handling by default and rely solely on the existing authenticated dead-tunnel detection in `connection_manager.go`.

### Proof of Concept
1. Establish a tunnel between nodes A and B.
2. Passively observe (or brute-force) B's `RemoteIndex` as sent in cleartext in any packet header from A→B or B→A.
3. From an attacker-controlled host capable of UDP source spoofing, craft a `header.RecvError` packet (`Type = header.RecvError`, `RemoteIndex = <observed index>`) with a spoofed source address equal to B's real UDP address, and send it to A.
4. `handleRecvError` on A finds the hostinfo via `QueryReverseIndex`, confirms the (spoofed) source matches the stored remote, and calls `f.closeTunnel(hostinfo)`, tearing down the legitimate tunnel with no cryptographic proof of authenticity. [2](#0-1)

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
