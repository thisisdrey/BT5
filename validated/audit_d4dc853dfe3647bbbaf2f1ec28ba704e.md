### Title
Unauthenticated `RecvError` packets let a spoofing attacker silently tear down any tunnel - (File: `outside.go`)

### Summary
The reported bug class is a state-clearing operation (`clearCrowdsourcers`) reachable by any unauthenticated caller, letting an attacker wipe critical protocol state and defeat the intended security process. In nebula, `header.RecvError` packets are processed as one of the explicitly "unencrypted packet" types, before any AEAD authentication, and their handler tears down the live tunnel (`HostInfo`) based only on a spoofable UDP source address and a wire-visible 32-bit index, with no cryptographic proof that the sender holds the session keys or a CA-signed certificate.

### Finding Description
In `readOutsidePackets`, `header.RecvError` is dispatched immediately after header parsing, in the same "Unencrypted packets" branch as the handshake type, and returns before any `hostinfo.ConnectionState.Decrypt` call: [1](#0-0) 

`handleRecvError` then looks up the `HostInfo` purely from `h.RemoteIndex` (an unauthenticated header field) and only checks that the claimed source address matches the `HostInfo`'s last known remote address before calling `f.closeTunnel(hostinfo)` and `f.handshakeManager.DeleteHostInfo(hostinfo)`: [2](#0-1) 

`closeTunnel` unconditionally deletes the `HostInfo` from the hostmap and, if it was the last hostinfo for that peer, wipes the learned lighthouse cache for the peer's VPN addresses: [3](#0-2) 

The only gate on this path is `acceptRecvErrorConfig.ShouldRecvError(addr)`, which is a coarse policy switch (`always`/`never`/`private`) and provides no authentication, and the address-match check itself only compares against the address stored in `HostInfo` (which is itself learned from unauthenticated UDP source addresses / roaming), not a certificate identity: [4](#0-3) 

This mirrors the Augur analog exactly: a state-clearing action (`clearCrowdsourcers` clearing dispute escrows; here, `closeTunnel`/`DeleteHostInfo` clearing the tunnel/session state) that should require possession of a proven credential (a filled dispute bond in Augur; a valid session key / CA-signed cert in nebula) but instead is reachable via a lightly-checked path exposed to any network attacker who can spoof or observe the peer's UDP address and guess/observe the numeric index.

### Impact Explanation
An attacker with no CA-signed certificate — only the ability to send UDP packets that appear to originate from a peer's known address/port (classic UDP source-spoofing, feasible from many networks lacking BCP38 filtering) and knowledge of the live `RemoteIndex` (observable by any on-path or off-path packet sniffing, or brute-forceable as it is only 32 bits and reused across the tunnel's lifetime) — can force `closeTunnel` + `DeleteHostInfo` on a victim's `HostInfo`. This is a remote state-poisoning/denial-of-service primitive: it silently destroys an established, authenticated tunnel and clears learned lighthouse state, forcing constant re-handshakes and disrupting connectivity, all without needing to ever complete a handshake or hold a valid certificate.

### Likelihood Explanation
Reaching this code requires only a crafted UDP packet with `header.RecvError` type and a `RemoteIndex` matching a live tunnel, and a source address the target believes matches the peer's remote endpoint. No decryption or certificate check gates this path (`outside.go:75-84`), and the only defense — the address match in `handleRecvError` — relies on network-layer information (source address) rather than cryptographic authentication, making this attack always reachable to a network-position attacker capable of spoofing UDP source addresses, matching the report's "reachable by anyone" characteristic.

### Recommendation
Require the `RecvError` message (or the closing/teardown decision it triggers) to be cryptographically authenticated — e.g., only honor it if it is delivered with a valid MAC/AEAD tag bound to the session, or require a signed nonce/challenge-response, similar to how `CloseTunnel` messages are authenticated via the encrypted `Message` channel. At minimum, do not let an unauthenticated `RecvError` packet trigger `DeleteHostInfo`/lighthouse cache clearing; instead use it only as a hint to trigger a rate-limited, authenticated re-handshake probe.

### Proof of Concept
1. Establish a live tunnel between `me` and `them` so `me` holds a `HostInfo` for `them` with `RemoteIndex = X` and `CurrentRemote = them's real udpAddr`.
2. An attacker (no cert, off-path but capable of spoofing UDP source address `them's real udpAddr`) sends a bare `header.RecvError` packet with `RemoteIndex = X` to `me`'s UDP listener.
3. `readOutsidePackets` routes it directly to `f.handleRecvError` without decryption (`outside.go:81-83`).
4. `handleRecvError` finds the matching `HostInfo` via `QueryReverseIndex(X)`, sees the spoofed address matches `hostinfo.GetRemote()`, and calls `f.closeTunnel(hostinfo)` plus `f.handshakeManager.DeleteHostInfo(hostinfo)` (`outside.go:557-575`), destroying the tunnel state that `them` never authorized to be torn down.

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
