### Title
Unauthenticated `RecvError` packet allows remote tunnel teardown via spoofed source address - (File: `outside.go`)

### Summary
The external report describes an attacker manipulating a Curve price oracle by taking an action that is not gated by the intended authorization check (`check_lock`) — a plain token transfer bypasses the lock and still influences the oracle's trusted state. The reachable analog in this codebase is `header.RecvError` handling: it is processed as an **unencrypted, unauthenticated** control message, before any handshake/decryption/certificate check, yet it can mutate trusted tunnel state (tear down an established tunnel) if the attacker can spoof a UDP packet whose source matches the victim's currently-known remote address.

### Finding Description
In `readOutsidePackets`, `header.RecvError` is dispatched straight to `f.handleRecvError` before any decryption or certificate verification occurs, alongside `header.Handshake` in the "Unencrypted packets" switch: [1](#0-0) 

`handleRecvError` looks up the hostinfo purely by the numeric `RemoteIndex` in the packet header (no AEAD tag, no signature) and compares only the UDP source address against the hostinfo's currently stored remote address: [2](#0-1) 

If the spoofed/attacker-controlled UDP source address happens to match `hostinfo.GetRemote()` (or the victim is behind a NAT/shared network the attacker can reach), the check `hr.IsValid() && hr != addr` passes, and the tunnel is torn down (`f.closeTunnel`) and the pending handshake entry deleted — all without ever presenting a valid Noise handshake or CA-signed certificate. The only defense is `RemoteIndex` needing to be a currently-valid 32-bit index and the source IP needing to match, neither of which requires possessing key material or a certificate.

This mirrors the reported bug class: an action outside the intended authorization gate (`check_lock`/handshake-authenticated path) is able to influence protected state (the oracle price / the tunnel's liveness state) because the code path that performs the mutation doesn't require the same proof of authorization as the "normal" path (a full handshake for tunnel establishment, or `controller_long`/`controller_short` calls for oracle price movement).

### Impact Explanation
An attacker with no certificate, no key material, and no established tunnel can force closure of an already-established Nebula tunnel between two legitimate peers by guessing/observing a valid `RemoteIndex` (32-bit, observable on the wire since it's sent in cleartext on every packet) and spoofing (or actually sending from) a UDP packet with the matching source `ip:port`. This is a remote state-poisoning / denial-of-service primitive: it disrupts an authenticated relationship using an unauthenticated packet, directly analogous to how the reported oracle could be poisoned by an action bypassing the intended lock.

### Likelihood Explanation
`RemoteIndex` is transmitted unencrypted in the header of every packet on the wire and is only 32 bits, making it observable/guessable by an on-path or off-path attacker who can also spoof/inject UDP packets (a routed/unencrypted UDP protocol has no built-in protection against source-address spoofing at the IP layer). The severity is gated by `acceptRecvErrorConfig.ShouldRecvError(addr)`, whose exact default policy could not be fully confirmed from the excerpts retrieved (defined in `interface.go`, which surfaced many matches for `ShouldRecvError`/`recv_error` config but wasn't fully read in this session) — this is a known gap in my verification.

### Recommendation
Require the `RecvError` message to be tied to something an unauthenticated attacker cannot forge — e.g., require it be sent from (and validated against) the exact learned remote and additionally validate a random nonce/nonce established during the handshake, or disable/ignore this cleartext teardown signal by default and only honor it after some form of authenticated confirmation (e.g., trigger a dead-tunnel probe/re-handshake instead of immediately deleting state).

### Proof of Concept
1. Two legitimate Nebula peers `A` and `B` complete a handshake and establish a tunnel; `A`'s hostinfo for `B` records `B`'s current remote `ip:port` and `RemoteIndex`.
2. An attacker observes (or brute-forces) `B`'s `RemoteIndex` from cleartext traffic and either spoofs `B`'s `ip:port` as the UDP source or is positioned to send from that same address (e.g., shared NAT).
3. Attacker sends a bare `header.RecvError` packet (no encryption, no handshake) with that `RemoteIndex` to `A`.
4. `handleRecvError` finds the hostinfo via `QueryReverseIndex`, sees `hr == addr`, and calls `f.closeTunnel(hostinfo)` plus deletes the pending handshake entry — tearing down `A`'s view of the tunnel to `B` without the attacker ever proving possession of a certificate or completing a handshake. [3](#0-2) [4](#0-3)

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
