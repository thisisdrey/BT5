### Title
Unauthenticated `RecvError` Packets Allow Anyone to Force Tunnel Teardown (Remote State Poisoning / DoS) - (File: `outside.go`)

### Summary
Nebula's `header.RecvError` message type is processed before any cryptographic authentication (no certificate check, no AEAD/HMAC verification), and the one address-based check that exists is a spoofable UDP source-address comparison. Any off-path or on-path attacker who can forge a `RecvError` packet — with no CA-signed certificate and no participation in the handshake — can force `f.closeTunnel(hostinfo)` and delete the peer's pending handshake state at will, exactly mirroring the reported bug class: an unprotected function that toggles security-relevant state and can be invoked by anyone, including via front-running/timing attacks against legitimate operations (here, tunnel re-establishment/roaming instead of liquidation).

### Finding Description
In `readOutsidePackets`, the `header.RecvError` type is dispatched to `f.handleRecvError` immediately, before any decryption or peer-certificate verification is required: [1](#0-0) 

`handleRecvError` looks up the target `hostinfo` purely from the cleartext `h.RemoteIndex` field via `QueryReverseIndex`, then performs only a weak sanity check against the packet's UDP source address before tearing the tunnel down: [2](#0-1) 

The guard `if hr.IsValid() && hr != addr` is the only thing standing between an arbitrary sender and `f.closeTunnel(hostinfo)` plus `f.handshakeManager.DeleteHostInfo(hostinfo)`. This is analogous to `ReserveFund.pause()`/`unpause()` being unprotected: a state-mutating operation (tunnel teardown, which is checked elsewhere for liveness/roaming decisions) is reachable by an unauthenticated party. Unlike the `CloseTunnel` message type (which is only processed after the packet passes AEAD decryption under an established `ConnectionState`, see `outside.go:164-166`), `RecvError` bypasses that authentication entirely by design (it's meant to be sent before a tunnel exists), but it still triggers teardown of an *already established* tunnel found via `QueryReverseIndex`.

Because `h.RemoteIndex` (the index value used to find the victim's `hostinfo` here) travels in cleartext in every packet header, any on-path observer — with no certificate, no CA trust, no handshake participation — can learn it and later spoof a `RecvError` packet (source-address spoofing over UDP is a well-known primitive not requiring a valid session) to force the check to pass and the tunnel to be dropped.

### Impact Explanation
An attacker who can observe (or blind-guess/replay) a valid `RemoteIndex` and spoof the sender's UDP source address can unilaterally force tunnel teardown on any live Nebula peer, causing:
- Denial of service (repeated forced re-handshakes / connection churn).
- Deletion of pending handshake state (`hm.DeleteHostInfo`), disrupting in-flight handshakes.
- Potential window-of-opportunity attacks: an attacker could time the teardown to coincide with sensitive operations (e.g., forcing a re-handshake right before/after a roaming event), similarly to how the original report describes forcing improper liquidation calculations by pausing/unpausing at will.

### Likelihood Explanation
The check relies solely on comparing a UDP source address to the stored `CurrentRemote`, which is spoofable, and the lookup key (`RemoteIndex`) is transmitted in cleartext in every packet, making it observable to any network-level attacker without needing a CA-issued certificate or completing a handshake. The `listen.accept_recv_error` config (`private`/`always`/`never`) can reduce exposure but defaults to `always`, per `recvErrorAlways` default in `reloadAcceptRecvError`. [3](#0-2) 

### Recommendation
`handleRecvError` should not act on the sender-supplied address alone. At minimum:
- Require this packet type to be authenticated (e.g., include a MAC computed with a key already established for that `hostinfo`, or ignore `RecvError` entirely for hostinfos that already have a fully-established `ConnectionState` and rely only on internal liveness/roaming logic).
- If backward compatibility requires keeping the current design, harden the check so a bare address match isn't sufficient to drop a live tunnel, and consider rate-limiting/logging repeated `RecvError` triggers per index.

### Proof of Concept
1. Observe (or otherwise learn) a live Nebula tunnel's `RemoteIndex` from any packet's cleartext header (e.g., by passively monitoring traffic on the path, or via a prior interaction).
2. Craft a `header.RecvError` packet with that `RemoteIndex` and send it with a spoofed UDP source address matching the victim's currently known remote endpoint (`hr`), targeting the other peer.
3. On the receiving side, `handleRecvError` finds the `hostinfo` via `QueryReverseIndex`, the address check passes (spoofed to match `hr`), and `f.closeTunnel(hostinfo)` plus `hm.DeleteHostInfo(hostinfo)` execute — tearing down a fully established, otherwise-healthy tunnel without any certificate or handshake participation by the attacker.

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

**File:** interface.go (L459-479)
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
```
