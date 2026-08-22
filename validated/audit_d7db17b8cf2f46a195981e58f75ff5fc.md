### Title
Unauthenticated `recv_error` packet can force-close relay-only tunnels because the sender address is not validated when no direct remote is known - (File: outside.go)

### Summary
`Interface.handleRecvError` (`outside.go:541-575`) tears down a tunnel in response to an unauthenticated, unencrypted `header.RecvError` control packet. Its only anti-spoofing check compares the packet's source address to the `HostInfo`'s currently known remote (`hostinfo.GetRemote()`), but that check is skipped entirely whenever the stored remote is not yet valid — which is the normal, persistent state for any tunnel established purely over a relay. This mirrors the reported `VaultAdapter.rate` bug class: an externally supplied, unvalidated address is used to gate a state-mutating action (here, tunnel teardown and hostinfo deletion) without verifying that the address actually belongs to the legitimate remote peer.

### Finding Description
`header.RecvError` packets are handled before any Noise/cert authentication takes place (`outside.go:81-84`, dispatched straight from `readOutsidePackets`), so any attacker on the path who knows a victim's UDP listening endpoint and a valid `RemoteIndex` (the header field is always sent in cleartext, including on relayed/data packets) can forge a `RecvError` packet from an arbitrary spoofed UDP source address.

```go
func (f *Interface) handleRecvError(addr netip.AddrPort, h *header.H) {
	...
	hostinfo := f.hostMap.QueryReverseIndex(h.RemoteIndex)
	if hostinfo == nil {
		...
		return
	}

	hr := hostinfo.GetRemote()
	if hr.IsValid() && hr != addr {
		f.l.Info("Someone spoofing recv_errors?", ...)
		return
	}

	f.closeTunnel(hostinfo)
	f.handshakeManager.DeleteHostInfo(hostinfo)
}
``` [1](#0-0) 

The only defense is `hr.IsValid() && hr != addr`. `HostInfo.GetRemote()` returns a zero `netip.AddrPort` (`IsValid() == false`) until `SetRemote` is called [2](#0-1) . `SetRemote` is only invoked on the direct (non-relayed) path during handshake completion: in `beginHandshake` and `continueHandshake`, `hostinfo.SetRemote(via.UdpAddr)` is guarded by `if !via.IsRelayed`; for relayed handshakes the code instead calls `hostinfo.relayState.InsertRelayTo(...)` and never sets `remote` [3](#0-2) , [4](#0-3) . Likewise `SetRemoteIfPreferred`, the only other setter, explicitly returns immediately for relayed traffic (`if via.IsRelayed { return false }`) [5](#0-4) .

Consequently, for any tunnel that is established and remains relay-only (no direct UDP path is ever learned — e.g., both peers are behind symmetric NAT and the relay is used indefinitely, which Nebula explicitly supports), `hr.IsValid()` is always `false`. The spoof check `hr.IsValid() && hr != addr` short-circuits to `false`, so the address-mismatch branch is never taken and `closeTunnel`/`DeleteHostInfo` execute unconditionally for a `RecvError` packet claiming to come from *any* source address, as long as the attacker supplies a `RemoteIndex` value that resolves via `QueryReverseIndex`.

### Impact Explanation
This lets an unauthenticated, off-path attacker (no CA-signed certificate, no established Noise session) force-terminate any relay-only tunnel by spoofing a single UDP packet, provided they can obtain the tunnel's `RemoteIndex` — a 32-bit value transmitted in cleartext in every packet header on that tunnel (including data and relay packets an on-path or same-network observer can see). This is a remote state-poisoning/DoS primitive: it repeatedly destroys relay tunnels, forcing costly re-handshakes and disrupting connectivity for hosts that cannot establish direct paths (the exact scenario `recv_error` acceptance/sending exists to optimize, per `listen.send_recv_error`/`accept_recv_error` config options noted in `CHANGELOG.md`). It does not itself grant traffic decryption or forgery, but it is a concrete unauthenticated remote-state-poisoning / connectivity-disruption impact directly analogous to the reported class (unvalidated address input driving unauthenticated mutation of protocol-critical state).

### Likelihood Explanation
Medium: the attacker needs to know a live `RemoteIndex` for a relay-only tunnel. This value is not secret — it appears in cleartext in the Nebula packet header on every packet sent over that tunnel (relayed data packets, punch traffic, etc.), so any attacker capable of observing traffic to/from the relay (a realistic on-path or shared-network position, or simply the relay operator/any peer that has previously handshaken and seen headers) can harvest it. No cryptographic material or valid certificate is required to exploit the bug once the index is known.

### Recommendation
- Short term: do not gate the spoof check on `hr.IsValid()`. When no learned/direct remote exists for a `HostInfo` (relay-only tunnels), `handleRecvError` should either refuse to act on unauthenticated `RecvError` packets, or additionally verify the packet arrived via the same relay path (checking against `hostinfo.relayState` / the relay peer's `HostInfo`) rather than trusting an arbitrary outside UDP source address.
- Long term: consider authenticating `RecvError` (e.g., binding it to the encrypted channel via a MAC keyed off session state) instead of relying purely on UDP source-address comparison, consistent with the existing `listen.send_recv_error`/`accept_recv_error` hardening options already present in the codebase.

### Proof of Concept
1. Establish two Nebula nodes, A and B, whose only connectivity path is via a relay node R (e.g., both behind symmetric NAT so no direct UDP path is ever learned) — a supported configuration per Nebula's relay system.
2. As an attacker able to observe any packet on the A↔R↔B relayed tunnel (e.g., sniffing on R's network, or being R itself, or observing punch/relay traffic), read the cleartext `RemoteIndex` field from A's outbound header.
3. Craft a UDP packet with `header.Type = header.RecvError`, `RemoteIndex` set to the observed value, and send it to A's listening UDP port from an arbitrary spoofed source address (not R's or B's real address).
4. On A, `handleRecvError` resolves the `HostInfo` via `QueryReverseIndex`, finds `hostinfo.GetRemote()` invalid (never set for the relay-only tunnel), skips the spoof check, and calls `f.closeTunnel(hostinfo)` plus `f.handshakeManager.DeleteHostInfo(hostinfo)`, tearing down the tunnel without any valid certificate or session on the attacker's part.

### Citations

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

**File:** hostmap.go (L769-783)
```go
func (i *HostInfo) GetRemote() netip.AddrPort {
	if p := i.remote.Load(); p != nil {
		return *p
	}
	return netip.AddrPort{}
}

// TODO: Maybe use ViaSender here?
func (i *HostInfo) SetRemote(remote netip.AddrPort) {
	// We copy here because we likely got this remote from a source that reuses the object
	if i.GetRemote() != remote {
		i.remote.Store(&remote)
		i.remotes.LearnRemote(i.vpnAddrs[0], remote)
	}
}
```

**File:** hostmap.go (L787-790)
```go
func (i *HostInfo) SetRemoteIfPreferred(hm *HostMap, via ViaSender) bool {
	if via.IsRelayed {
		return false
	}
```

**File:** handshake_manager.go (L791-794)
```go
	hostinfo.remotes = f.lightHouse.QueryCache(vpnAddrs)
	if !via.IsRelayed {
		hostinfo.SetRemote(via.UdpAddr)
	}
```

**File:** handshake_manager.go (L885-889)
```go
	if !via.IsRelayed {
		hostinfo.SetRemote(via.UdpAddr)
	} else {
		hostinfo.relayState.InsertRelayTo(via.relayHI.vpnAddrs[0])
	}
```
