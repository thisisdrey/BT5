### Title
Spoofed `RecvError` packet can tear down a tunnel with no valid remote recorded - (File: `outside.go`, `handshake_manager.go`, `hostmap.go`)

### Summary
The reported `HanjiLOB.changeMarketMakerAddress` bug lets anyone set a privileged value as long as the current stored value is the zero value, because the authorization check is skipped entirely for that branch. The same bug class exists in Nebula's `RecvError` handling: the anti-spoofing comparison in `Interface.handleRecvError` is only performed when the hostinfo already has a recorded remote (`hr.IsValid()`); when the recorded remote is still the zero value (which is the case for relayed tunnels that have not yet learned a direct address), the check is skipped entirely and the tunnel is torn down unconditionally on receipt of a single unauthenticated, unencrypted packet.

### Finding Description
`RecvError` packets are handled before any decryption or certificate validation, in the "Unencrypted packets" branch of the inbound packet path: [1](#0-0) 

The handler is: [2](#0-1) 

The critical logic is:
```go
hr := hostinfo.GetRemote()
if hr.IsValid() && hr != addr {
    f.l.Info("Someone spoofing recv_errors?", ...)
    return
}
f.closeTunnel(hostinfo)
```
This mirrors the `marketmaker` bug exactly: the authenticity check ("is this packet actually coming from the tunnel's real remote?") is only enforced *if* `hr` is already set (non-zero). If `hr` is still the zero value, the code assumes there is nothing to protect and lets the request through unconditionally — just as `changeMarketMakerAddress` assumed that a zero `marketmaker` address meant "safe to let anyone claim it."

`hostinfo.GetRemote()` returns the zero `netip.AddrPort` until `SetRemote` has been called for that hostinfo: [3](#0-2) 

For relayed handshakes, `SetRemote` is deliberately *not* called (the tunnel has no direct UDP endpoint yet, only a relay path): [4](#0-3) [5](#0-4) 

So for any tunnel that is established purely via a relay (no direct path learned yet), `hostinfo.GetRemote()` stays invalid indefinitely, and `handleRecvError` will accept a `RecvError` packet claiming to originate from *any* spoofed source `addr`, as long as the attacker supplies a `RemoteIndex` (`h.RemoteIndex`) that resolves via `QueryReverseIndex` to that hostinfo.

The `RemoteIndex` value is not secret: index values are exchanged in the cleartext header of handshake packets (the header is never encrypted; only the Noise payload is), so any attacker positioned to observe UDP traffic between two Nebula nodes — without holding any CA-signed certificate themselves — can learn the index and later forge a bare 8-byte `RecvError` packet with a spoofed source address to hit this code path.

### Impact Explanation
An attacker with no valid Nebula certificate, who can observe (or otherwise learn) a live `RemoteIndex` for a relayed tunnel, can forge a single unauthenticated UDP packet to unconditionally invoke `f.closeTunnel(hostinfo)`: [6](#0-5) 
This tears down the tunnel and also removes it from the pending handshake map, forcing repeated re-handshakes. This is a remote state-poisoning / denial-of-service primitive that bypasses the tunnel's cryptographic authentication entirely, because the code path is reached before any certificate or session-key check — exactly the class of "unauthenticated actor exploits an assume-zero-means-safe branch" described in the reported bug.

### Likelihood Explanation
Exploitation requires: (1) the target tunnel to be relayed and to not yet have learned a direct remote (a real, not uncommon, operational state, e.g., peers behind restrictive NATs relying on relays), and (2) knowledge of the correct 32-bit `RemoteIndex`, obtainable by an attacker able to observe handshake traffic on the path (headers are unencrypted) or otherwise infer it. `acceptRecvErrorConfig`/`ShouldRecvError` only rate-limits, it does not authenticate, so it does not block a single spoofed attempt. This is a moderate-likelihood issue: it needs network visibility of index values but no cryptographic material at all.

### Recommendation
Do not treat an unset/zero `GetRemote()` as "no check needed." When `hostinfo.GetRemote()` is invalid, either refuse to act on `RecvError` for that hostinfo, or require some other proof of legitimacy (e.g., only honor `RecvError` from an address that matches one of the tunnel's already-verified/candidate remotes, or ignore `RecvError` entirely for hostinfos with no direct remote learned yet). More generally, any comparison of the form "if stored value is zero, skip the check" should be replaced with an explicit affirmative allow-list/verification, not an implicit bypass.

### Proof of Concept
1. Set up two Nebula nodes, A and B, that can only reach each other through a relay `R` (so neither ever calls `hostinfo.SetRemote` for the peer, leaving `GetRemote()` invalid on both ends).
2. Have an unauthenticated network observer (no Nebula certificate) sniff the `CreateRelayRequest`/handshake traffic between A and the relay to learn a valid `RemoteIndex` for A's hostinfo referencing B (index values live in the cleartext packet header).
3. Craft a minimal `RecvError` UDP packet (`header.Encode(..., header.RecvError, 0, learnedIndex, 0)`) and send it to A from an arbitrary spoofed source address.
4. Observe that `Interface.handleRecvError` finds the hostinfo via `QueryReverseIndex`, sees `hr.IsValid() == false`, skips the spoofing check, and calls `f.closeTunnel(hostinfo)`, tearing down A's relayed tunnel to B without ever presenting a valid certificate.

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

**File:** outside.go (L541-562)
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

```

**File:** outside.go (L571-574)
```go

	f.closeTunnel(hostinfo)
	// We also delete it from pending hostmap to allow for fast reconnect.
	f.handshakeManager.DeleteHostInfo(hostinfo)
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
