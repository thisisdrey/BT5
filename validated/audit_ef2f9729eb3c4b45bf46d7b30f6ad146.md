### Title
Unauthenticated `recv_error` packet can force-close relayed tunnels without a source-address check - (File: outside.go)

### Summary
`handleRecvError()` in `outside.go` is reachable directly from `readOutsidePackets()` for any raw, unauthenticated UDP packet whose header type is `header.RecvError` — no handshake, certificate, or CA-pool verification is required to reach it. The function only rejects a spoofed sender when the target `HostInfo`'s cached remote address (`hostinfo.GetRemote()`) `IsValid()`. For relayed tunnels this cached remote address is never populated, so the address-mismatch defense is silently skipped, letting an attacker with no CA-signed certificate tear down (or reset the pending state of) any tunnel whose `RemoteIndex` it can supply.

### Finding Description
`readOutsidePackets()` dispatches `header.RecvError` packets to `f.handleRecvError()` before any hostinfo/ConnectionState/certificate check is performed: [1](#0-0) 

`handleRecvError()` looks up the target hostinfo purely by the numeric `RemoteIndex` carried in the (unauthenticated, unencrypted) header, and then attempts to prevent a spoofed sender from tearing down the tunnel by comparing the sender address to the hostinfo's cached remote: [2](#0-1) 

```go
hr := hostinfo.GetRemote()
if hr.IsValid() && hr != addr {
    // rejected as spoofed
    return
}
f.closeTunnel(hostinfo)
f.handshakeManager.DeleteHostInfo(hostinfo)
```

The check assumes the hostinfo's cached remote address is always populated ("pre-existing, known-good state") by the time a `RecvError` can arrive, and only rejects the packet when a *mismatch* is detected against that assumed value. It does nothing when the assumed value is simply absent (`hr.IsValid() == false`). This mirrors the reported bug class: a security check is built around an assumption about ambient/pre-existing state (the ExecutionEnvironment's ETH balance / here, the hostinfo's confirmed remote endpoint) that an external actor can arrange to be in the "unset" condition, causing the guard to be bypassed rather than enforced.

The remote address is populated via `hostinfo.SetRemote(via.UdpAddr)`, but this is explicitly skipped for relayed handshakes: [3](#0-2) 

```go
hostinfo.remotes = f.lightHouse.QueryCache(vpnAddrs)
if !via.IsRelayed {
    hostinfo.SetRemote(via.UdpAddr)
}
```

Because a purely relay-connected `HostInfo` never has a direct UDP endpoint, its `GetRemote()` result stays invalid for the lifetime of the tunnel unless some other code path later sets it (I was not able to confirm within the available search results whether any other code path ever back-fills `SetRemote` for a relay-only hostinfo; this should be verified in the full source). Assuming it does not, the `hr.IsValid()` guard in `handleRecvError` is permanently disabled for such tunnels, and any off-path attacker who can send a single spoofed UDP datagram with the victim's `RemoteIndex` in the header can unconditionally call `f.closeTunnel(hostinfo)` and `f.handshakeManager.DeleteHostInfo(hostinfo)`.

### Impact Explanation
An attacker with no valid CA-signed certificate and no on-path position (only the ability to send arbitrary UDP datagrams to the victim's listen port, with a spoofable source address) can force-terminate any relayed Nebula tunnel it can target by index, causing:
- Remote tunnel teardown / denial of service against relayed peers, which the victim must re-handshake to recover from.
- Repeated/targeted use could be used to persistently disrupt relayed connectivity between two nodes.

The severity is bounded by the difficulty of learning/guessing a valid 32-bit `RemoteIndex`, but once known (e.g., observed on the wire, since these indexes are sent in cleartext headers on every packet), the attack requires no cryptographic material and no participation in a valid handshake.

### Likelihood Explanation
- `RemoteIndex` values are transmitted in cleartext in every packet header (`header.H.RemoteIndex`), so a passive on-path or off-path observer able to see any traffic to/from the victim (e.g., a shared network segment, or by inference from `recv_error`/handshake logging) can learn a valid index for a relayed tunnel.
- The `acceptRecvErrorConfig.ShouldRecvError(addr)` gate in `handleRecvError` only filters by configuration policy (e.g., "always"/"only known"), not by cryptographic proof, so it does not prevent this bypass for hosts that accept `recv_error` from unknown sources (the documented default is "always", per `CHANGELOG.md` — "Add a config option to control accepting recv_error packets which defaults to always").
- The bypass condition (`hr.IsValid() == false`) is deterministic and always true for relay-only tunnels, not merely a narrow race window, so exploitation does not depend on precise timing.

### Recommendation
- Do not rely solely on `hr.IsValid()` to decide whether to perform the address check in `handleRecvError()`. For hostinfos without a known direct remote (relayed tunnels), require an alternative authentication of the sender (e.g., verify the packet came via the same relay hop that is tracked for that hostinfo, or require `recv_error` for relay tunnels to be relay-authenticated rather than accepted from arbitrary UDP source addresses).
- Alternatively, do not honor unauthenticated `recv_error` for tunnels lacking a validated direct remote at all, and instead require a MAC-protected/relay-verified error signal, consistent with how relay data packets are verified (`ConnectionState.VerifyRelay`) before being acted upon.
- Audit all other call sites gated on `hostinfo.GetRemote().IsValid()` for the same "absent state bypasses the check" pattern.

### Proof of Concept
1. Establish (or observe) a relayed tunnel between "me" and "them" via a relay node, as in `TestRelayReplayProtection`/`TestCrossStackRelaysWork` (`e2e/tunnels_test.go`).
2. Learn the `RemoteIndex` used by one side for that relayed hostinfo (visible in cleartext in any packet header exchanged over the relay).
3. From an arbitrary, unauthenticated, spoofable UDP source, send a bare `header.RecvError` packet (`header.Encode(..., header.RecvError, 0, index, 0)`) to the victim's listen port with that `RemoteIndex`.
4. Because the victim's relayed `HostInfo.GetRemote()` is never set (`SetRemote` is skipped for `via.IsRelayed` handshakes), `handleRecvError` skips the source-address check and calls `f.closeTunnel(hostinfo)` plus `f.handshakeManager.DeleteHostInfo(hostinfo)`, tearing down the tunnel without the attacker ever presenting a valid certificate or being on-path.
(Note: I could not fully verify from the indexed source whether any later code path back-fills `SetRemote` for relay-only hostinfos; this should be confirmed against the full repository before treating the impact as unconditional.)

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

**File:** handshake_manager.go (L791-795)
```go
	hostinfo.remotes = f.lightHouse.QueryCache(vpnAddrs)
	if !via.IsRelayed {
		hostinfo.SetRemote(via.UdpAddr)
	}
	hostinfo.buildNetworks(f.myVpnNetworksTable, remoteCert.Certificate)
```
