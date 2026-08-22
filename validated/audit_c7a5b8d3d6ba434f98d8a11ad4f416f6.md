### Title
Unauthenticated `recv_error` packet allows spoofed teardown of established tunnels - (File: outside.go)

### Summary
Nebula's `RecvError` control message is processed without any handshake, certificate, or encryption authentication. An attacker who can spoof the source UDP address of an existing peer and who knows (or observes, since headers are unencrypted) the target's local index can force `handleRecvError` to tear down a legitimate, working tunnel, exactly analogous to the referenced Lido bug in which an unprivileged caller could increment shared state (`KEYS_OP_INDEX_POSITION`) to disrupt a legitimate downstream operation (`depositBufferedEther()`).

### Finding Description
Inbound `header.RecvError` packets are dispatched straight from `readOutsidePackets` before any decryption or hostinfo/cert validation occurs: [1](#0-0) 

`handleRecvError` then looks up the hostinfo purely by the (unencrypted, wire-visible) `RemoteIndex` field and gates only on the sender's advertised UDP address matching the hostinfo's currently known remote: [2](#0-1) 

If that address check passes, it unconditionally calls `f.closeTunnel(hostinfo)` and also deletes it from the pending handshake map — no certificate, no CA-pool verification, no Noise/AEAD authentication is involved at any point in this path. The only defense is comparing the packet's *source UDP address* to the hostinfo's currently believed remote endpoint, which is trivially defeated by IP spoofing since UDP has no built-in source authentication, and by the fact that the `RemoteIndex` used to select the target hostinfo travels in cleartext in every packet header (`header.Encode`), making it observable to any attacker positioned to see traffic (or in various off-path spoofing/prediction scenarios).

This mirrors the class of bug described in the report: a state-mutating operation reachable by an unauthorized/unauthenticated actor that can be abused to disrupt or block a legitimate, security-critical operation for other, honest participants.

### Impact Explanation
An attacker satisfying the address-spoofing precondition can force termination of an arbitrary victim's active Nebula tunnel at will, repeatedly, without ever holding a CA-signed certificate or completing a handshake. This is a remote denial-of-service against the mesh network's availability: legitimate encrypted traffic between two authenticated peers is disrupted by a purely unauthenticated wire-level message.

### Likelihood Explanation
Exploitation requires (a) knowledge of a valid `RemoteIndex` for the target tunnel, which is visible in the cleartext header of any packet exchanged on that tunnel, and (b) the ability to spoof the peer's source UDP address as seen by the target (feasible on many UDP paths/NATs, and by design in the `recvErrorConfig` handling which only compares source-address, not proof-of-possession of any secret). The `sendRecvErrorConfig`/`acceptRecvErrorConfig` settings (`recvErrorAlways`, `recvErrorPrivate`, `recvErrorNever`) govern whether this vector is enabled at all, so likelihood is configuration-dependent, but where enabled the attack is a single crafted, unauthenticated UDP packet.

### Recommendation
Do not act on `RecvError` packets to tear down tunnels without further corroboration. At minimum, require that a `RecvError` be authenticated (e.g., signed/MACed similarly to other post-handshake messages, or correlated against outbound traffic actually sent to that specific index/address pair within a tight time window) before calling `closeTunnel`/`DeleteHostInfo`. Consider rate-limiting or requiring multiple independent-source confirmations before disconnecting an established tunnel, and prefer disabling acceptance of `RecvError` from public/untrusted address ranges by default (`recvErrorPrivate`/`recvErrorNever`).

### Proof of Concept
1. Observe (or otherwise learn) the cleartext header of an established Nebula tunnel packet between victim A and victim B to obtain `RemoteIndex` for A's tunnel to B and A's current UDP address as understood by B.
2. Craft a `header.RecvError` packet: `header.Encode(buf, header.Version, header.RecvError, 0, <A's index at B>, 0)`.
3. Spoof the packet's source UDP address to match A's currently known remote address at B (per `hostinfo.GetRemote()`), matching the check at `outside.go:564`.
4. Send the crafted packet to B. `handleRecvError` finds the hostinfo via `QueryReverseIndex`, the spoof check passes, and B calls `f.closeTunnel(hostinfo)` plus `f.handshakeManager.DeleteHostInfo(hostinfo)`, tearing down the legitimate tunnel — repeatable at will to keep the tunnel from ever staying established.

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
