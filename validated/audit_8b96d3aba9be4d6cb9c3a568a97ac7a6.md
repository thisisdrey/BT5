### Title
Unauthenticated `RecvError` packet allows a spoofing attacker with no valid certificate to force-teardown an established tunnel - ([File: outside.go])

### Summary
`RecvError` packets are processed by `Interface.readOutsidePackets` before any decryption or certificate-derived authentication is performed, and the only check `handleRecvError` applies is that the packet's *source UDP address* matches the address Nebula has on record for that `HostInfo`. Because the header (`RemoteIndex`) is transmitted in cleartext and UDP source addresses are trivially spoofable/observable by an on-path or off-path attacker who can guess/observe traffic between two peers, this lets an attacker with no CA-signed certificate force an already-established, mutually-authenticated tunnel to be torn down (`f.closeTunnel(hostinfo)` and removed from the pending handshake map), analogous to the reported class of "irreversible state transition performed without adequate authorization checks."

### Finding Description
In `outside.go`, `readOutsidePackets` dispatches unencrypted/pre-authentication message types immediately after header parsing and before any crypto verification: [1](#0-0) 

```
switch h.Type {
case header.Handshake:
    f.handshakeManager.HandleIncoming(via, packet, h)
    return
case header.RecvError:
    f.handleRecvError(via.UdpAddr, h)
    return
}
```

`handleRecvError` then only compares the packet's claimed `RemoteIndex` (looked up in the hostmap) against the *source address of the incoming UDP packet*: [2](#0-1) 

```go
func (f *Interface) handleRecvError(addr netip.AddrPort, h *header.H) {
	if !f.acceptRecvErrorConfig.ShouldRecvError(addr) {
		...
		return
	}
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
	// We also delete it from pending hostmap to allow for fast reconnect.
	f.handshakeManager.DeleteHostInfo(hostinfo)
}
```

The `RecvError` header carries no MAC, signature, or any cryptographic binding to the peer's certificate/key (per the on-wire header layout, `RemoteIndex` and `MessageCounter` are plain 32-bit/64-bit fields with no authentication tag): [3](#0-2) 

The `RemoteIndex` value is also visible in cleartext on every packet header (handshake packets and message packets alike carry it unencrypted in the 16-byte header before the encrypted payload), so any network observer between the two Nebula nodes can learn a live tunnel's index without possessing any certificate. Combined with UDP source-address spoofing (or simply being on-path and relaying/forging packets with the victim's source address), an attacker can:
1. Observe (or otherwise learn) a victim's currently active `RemoteIndex` for a tunnel to peer B.
2. Construct a bare `RecvError` header packet with that index.
3. Spoof the UDP source address to match peer B's currently-known remote (`hr`), satisfying the `hr != addr` check.
4. Send it to peer A, which is not filtered by nebula's own firewall (this pre-dates hostinfo/firewall lookup) nor validated cryptographically.

This causes `f.closeTunnel(hostinfo)` to run, deleting the established `HostInfo` from the main hostmap, and `f.handshakeManager.DeleteHostInfo` removes any pending state too — a state-poisoning/DoS action performed entirely by an unauthenticated party.

This is directly analogous to the reported smart-contract bug class: an operation ("adding funds"/here, "tearing down an already-established, authenticated tunnel") is permitted on state that should be considered protected/closed to unauthorized actors, because the guarding check (distribution-not-closed / certificate-authenticated-sender) is missing or insufficient (only a spoofable IP comparison instead of a cryptographic proof), and the resulting state change (closed distribution holding stuck funds / torn-down tunnel) cannot be reached through the normal, protected path.

### Impact Explanation
An attacker with no valid Nebula certificate can force any currently-established tunnel between two arbitrary peers to be torn down remotely, without needing to break the noise/IX handshake or possess valid key material — only knowledge of a live `RemoteIndex` (obtainable via passive observation of on-wire headers) and the ability to spoof the peer's source UDP address (or be positioned on-path). This is a remote-state-poisoning / denial-of-service primitive: it silently kills active tunnels, forcing repeated re-handshakes and enabling sustained disruption of the mesh, all pre-authentication. This maps to the "remote state poisoning" and "remote crash/DoS impact" categories called out as acceptable analog impact.

### Likelihood Explanation
Likelihood is nontrivial but requires: (1) the attacker to be able to spoof UDP source IP:port toward the target Nebula node (feasible on many networks lacking egress/ingress filtering, or when on-path), and (2) knowledge of the victim tunnel's `RemoteIndex`, which is sent unencrypted in every packet header and thus observable to any passive on-path observer. No certificate, private key, or successful handshake is required by the attacker at any point — only spoofing and observation, both of which are within scope for an "attacker with no CA-signed certificate."

### Recommendation
Do not act on `RecvError` (or any other type) solely based on comparing the packet's spoof-able source address to a stored remote. At minimum:
- Require some proof-of-possession tied to the session (e.g., a value derived from the session's established symmetric key/MAC) before honoring a `RecvError`-triggered teardown, similar to how `Message`/`Test`/`CloseTunnel` types are gated behind `hostinfo.ConnectionState.Decrypt`/verification.
- Alternatively, rate-limit and treat `RecvError` purely as a hint to retry rather than an unconditional trigger for `closeTunnel`/`DeleteHostInfo`, requiring corroboration (e.g., a subsequent handshake failure) before tearing down state.
- Ensure any process that can transition established/authenticated tunnel state to "torn down" enforces the same authentication guarantees as the data path, closing the gap between "cheap to spoof control packet" and "expensive to reach cryptographic state."

### Proof of Concept
1. Establish tunnel A↔B normally (mutual cert-based handshake).
2. As attacker C (holding no valid certificate), passively observe cleartext headers exchanged between A and B (any `Message`/`Handshake` packet reveals `RemoteIndex` for the tunnel, e.g., the value A uses to address B, or vice versa) — this is possible purely by sniffing UDP traffic on the path, no decryption needed.
3. Craft a raw UDP packet using `header.Encode(..., header.RecvError, 0, <observed RemoteIndex>, 0)` (see `sendRecvError` for the exact construction) and send it to A, spoofing the source address to match B's `GetRemote()` value on A's `HostInfo` (which is A's known UDP endpoint for B — learnable the same way).
4. A's `readOutsidePackets` dispatches straight to `handleRecvError` before any decryption; the `hr != addr` check passes because the spoofed source matches; `f.closeTunnel(hostinfo)` and `f.handshakeManager.DeleteHostInfo(hostinfo)` execute, tearing down A's side of the tunnel with zero certificate/key material presented by the attacker.

Note: I was unable to fully verify the exact rate-limiting behavior of `acceptRecvErrorConfig.ShouldRecvError` (defined in `interface.go`, not fully inspected in this session) — this may mitigate high-frequency abuse but does not close the fundamental authentication gap, since a single spoofed packet is sufficient to tear down one tunnel.

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

**File:** header/header.go (L10-27)
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

type m = map[string]any

const (
	Version uint8 = 1
	Len           = 16
)
```
