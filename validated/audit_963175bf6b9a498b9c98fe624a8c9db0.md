## handleRecvError trusts an unauthenticated, unencrypted `RecvError` header field (remote UDP index) to tear down an established, authenticated tunnel - (File: outside.go)

### Summary
`header.RecvError` packets are processed in `readOutsidePackets` before any decryption or certificate/session verification — they only carry a plaintext `RemoteIndex` field [1](#0-0) . `handleRecvError` then looks up the hostinfo purely by that attacker-supplied index and, if the source UDP address happens to match the hostinfo's currently known remote, immediately tears the tunnel down and deletes the pending handshake state [2](#0-1) . There is no cryptographic proof that the sender is the actual authenticated peer holding that index — the only "authorization" is a plaintext index guess plus an IP/port match, which is attacker-controllable via UDP source-address spoofing on many networks. This mirrors the reported Solana bug: a privileged action (`mint_gorples`/here "tear down and force re-handshake of a live tunnel") is gated on a value the caller supplies (`mint_authority` pubkey / `RemoteIndex`) rather than on cryptographic proof of identity (a real `is_signer` check / a proof that the sender is bound to the encrypted session).

### Finding Description
In the packet dispatch loop, `header.RecvError` is handled in the "Unencrypted packets" branch, i.e., strictly before the Noise-authenticated decryption path and before any certificate is consulted:
```
switch h.Type {
case header.Handshake: ...
case header.RecvError:
    f.handleRecvError(via.UdpAddr, h)
    return
}
``` [1](#0-0) 

`handleRecvError` resolves the hostinfo solely by an attacker-supplied 32-bit `RemoteIndex` and compares only the UDP source address against what is already stored (which is itself learned data, not a cryptographic secret):
```
hostinfo := f.hostMap.QueryReverseIndex(h.RemoteIndex)
...
hr := hostinfo.GetRemote()
if hr.IsValid() && hr != addr {
    f.l.Info("Someone spoofing recv_errors?", ...)
    return
}
f.closeTunnel(hostinfo)
f.handshakeManager.DeleteHostInfo(hostinfo)
``` [3](#0-2) 

There is a `ShouldRecvError` rate/acceptance gate [4](#0-3) , but that only throttles by source address; it does not authenticate that the packet actually originated from the genuine remote peer holding the referenced session. Just as the Solana program checked `config.contracts.contains(&mint_authority.key())` without requiring `mint_authority` to be a signer, Nebula checks "index exists in hostmap" + "source addr matches" without requiring the packet to be authenticated under the session's Noise/AEAD key — the one mechanism that would prove the sender actually possesses the peer's private key material.

### Impact Explanation
An attacker who can spoof UDP source addresses (or who is on-path/NAT-shared with a peer) and who can guess or observe a peer's 32-bit local index can force termination of any active, legitimately-authenticated tunnel and clear its pending handshake state, without holding any CA-signed certificate or completing any handshake themselves. This is a remote state-poisoning / denial-of-service primitive against the overlay: it can be used to repeatedly disrupt tunnels between two fully mutually-authenticated hosts, undermining the "mutually authenticated" guarantee central to Nebula's design.

### Likelihood Explanation
Indexes are 32-bit, generated pseudo-randomly and exchanged in cleartext-visible handshake headers, so on-path attackers (or malicious infra between peers, e.g., shared NAT/ISP) can trivially observe valid `RemoteIndex` values from the handshake exchange and immediately race a spoofed `RecvError` from the appropriate source address before/while the tunnel is active — no cryptographic secret or valid certificate is required, only address spoofing capability and a directly-observed index.

### Recommendation
Do not act on `RecvError` before cryptographic verification. At minimum: (1) require that `RecvError` handling be authenticated (e.g., wrapped/HMAC'd under the tunnel's derived Noise key or otherwise bound to the encrypted session rather than to a plaintext index+source-IP match), and/or (2) treat unauthenticated `RecvError` purely as a hint to re-probe/re-handshake rather than as sufficient grounds to immediately `closeTunnel`/`DeleteHostInfo`, and (3) rate-limit and log more aggressively per remote index, not just per source address.

### Proof of Concept
1. Observe (via network position or side channel) an in-flight handshake between hosts `A` and `B`, capturing `B`'s local index that `A` uses as `RemoteIndex` when addressing `B`.
2. From a spoofed UDP source address matching `A`'s known/expected remote address (or from a position that shares `A`'s NAT egress), send a crafted `header.RecvError` packet to `B` with `RemoteIndex` set to the observed index.
3. `B`'s `readOutsidePackets` dispatches directly to `handleRecvError` without decryption; `QueryReverseIndex` finds the live hostinfo, the spoofed source address matches the stored remote, and `B` calls `closeTunnel` + `DeleteHostInfo`, tearing down the authenticated tunnel with no cryptographic proof of sender identity.

Note: I could not fully verify from the indexed code how strongly UDP source-address spoofing is mitigated at the transport layer (e.g., OS-level anti-spoofing, `ShouldRecvError` internals) since `udp` package internals were not part of the retrieved context; a full Devin session with complete repo access would be needed to confirm end-to-end exploitability details (e.g., exact `ShouldRecvError` rate-limit semantics) if deeper validation is required.

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
