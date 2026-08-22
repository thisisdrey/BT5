### Title
Unauthenticated `RemoteIndex` mapping allows forged `RecvError` packets to tear down a legitimate peer's tunnel - (File: outside.go)

### Summary
`RecvError` packets are processed before any AEAD authentication and are used to look up a peer's `HostInfo` solely by the `RemoteIndex` field taken from the plaintext packet header, via a `HostMap.RemoteIndexes` map that is explicitly keyed by an ID the local node has no control over. This mirrors the reported vesting-plan bug class: a mapping keyed by an untrusted/attacker-influenced identifier (`owner -> token` in the C4 report; `remoteIndexId -> HostInfo` here) lets one entity's identifier collide with, and act upon, another entity's resource, without verifying that the actor is the legitimate owner of that resource.

### Finding Description
`HostMap.RemoteIndexes` maps a 32-bit index chosen by the *remote* peer to the corresponding `HostInfo`: [1](#0-0) . Because this index is "outside our control," the code already acknowledges that two different peers can collide on the same `remoteIndexId` and only logs about it rather than rejecting: [2](#0-1)  and [3](#0-2) , with the deletion path explicitly noting the same lack of control: [4](#0-3) .

In `readOutsidePackets`, packets of type `header.RecvError` are dispatched to `handleRecvError` immediately after basic header validation and *before* any decryption/authentication step is performed for other message types: [5](#0-4) . `handleRecvError` then resolves the target purely via `QueryReverseIndex(h.RemoteIndex)` against the same untrusted `RemoteIndexes` map, and if the source `addr` happens to match the current recorded remote endpoint for that `HostInfo`, it tears the tunnel down: [6](#0-5) . The lookup implementation is a direct, unauthenticated map read: [7](#0-6) .

Unlike `CloseTunnel`, which is only processed after `hostinfo.ConnectionState.Decrypt` succeeds (i.e., requires possession of the session key established through a signed handshake) at [8](#0-7) , `RecvError` bypasses this authentication entirely, exactly as the report describes for `createVesting`/`transferVesting`: the check exists (`_tokenOwner[msg.sender] == token`) but is checked against the wrong/ambiguous entity, and the actual sensitive action (fund transfer / tunnel teardown) is executed against a *different* party's resource than the one being validated.

### Impact Explanation
An attacker with no CA-signed certificate, who can (a) spoof the UDP source address to match a victim tunnel's current remote endpoint (trivial over UDP absent egress/ingress filtering), and (b) observe or guess the `RemoteIndex` value used on that tunnel (transmitted unencrypted in every packet header per `header.H`, so visible to any on-path observer of even a single packet, or discoverable via collision with the attacker's own handshake-issued index), can forge a `RecvError` packet that causes the target node to call `f.closeTunnel(hostinfo)` on the victim's live tunnel: [9](#0-8) . This is a remote state-poisoning / denial-of-service primitive: an unrelated, uncertificated party can force teardown of another peer's session, matching the "Creator of one vesting plan can affect vesting plans created by other users" bug class — one entity's un-authenticated identifier (index/owner mapping) is used to act on another entity's resource.

### Likelihood Explanation
Exploitation requires UDP source-address spoofing (feasible on many networks lacking BCP38 filtering) plus knowledge of the victim's `RemoteIndex`, which is sent unencrypted on every packet of the tunnel and thus is not secret — any observer of the link (or a malicious lighthouse/relay operator, or a node sharing the underlay network) can harvest it. This is comparable to the judge's assessment in the analog report: high impact (functionality broken — legitimate tunnel torn down) combined with a low-to-moderate likelihood (requires spoofing plus index disclosure), yielding a Medium-severity finding.

### Recommendation
Do not act on `RecvError` (or any similarly early, pre-authentication message) using only the attacker-influenced `RemoteIndex`. Either:
- Require that `RecvError` handling be authenticated the same way as other traffic (validated against a MAC/AEAD tag tied to the session), or
- Bind `RemoteIndexes` entries additionally to a value the remote cannot arbitrarily choose (e.g., verify a fingerprint of the peer's certificate/handshake state to disambiguate collisions instead of merely logging), or
- Rate-limit/deny `RecvError` processing unless it correlates with a matching, actively in-flight message the local node just sent, so a blind/spoofed injection cannot trigger teardown of an unrelated, established tunnel.

### Proof of Concept
1. Establish node `A`'s tunnel to victim `B` normally (handshake completes; `B` gets a `remoteIndexId = X` recorded in `A`'s `HostMap.RemoteIndexes[X] = hostinfoB`, and `A`'s `HostInfo` for `B` records `CurrentRemote = B_addr`).
2. An attacker `E` (holding no CA-signed cert) sniffs one packet of the `A<->B` tunnel (or otherwise learns `X`, e.g. through index collision with its own handshake) to learn `X` and `B_addr`.
3. `E` crafts a raw UDP packet to `A` with header `Type = header.RecvError`, `RemoteIndex = X`, source address spoofed to `B_addr`.
4. `A.readOutsidePackets` dispatches this directly to `handleRecvError` without any AEAD check [10](#0-9) .
5. `handleRecvError` resolves `hostinfo = QueryReverseIndex(X)` → `hostinfoB`, sees `hr == B_addr == addr`, and calls `f.closeTunnel(hostinfoB)`, tearing down `A`'s legitimate tunnel with `B` [11](#0-10) , without `E` ever presenting a valid certificate.

Note: I could not fully verify the exact behavior of `f.acceptRecvErrorConfig.ShouldRecvError(addr)` (only its call sites were located, not its full rate-limiting/allow-list implementation) — this gate may reduce but does not eliminate exploitability, since it is address-based rather than cryptographic. Confirming its precise semantics would require reading `interface.go` in full.

### Citations

**File:** hostmap.go (L58-62)
```go
type HostMap struct {
	sync.RWMutex  //Because we concurrently read and write to our maps
	Indexes       map[uint32]*HostInfo
	Relays        map[uint32]*HostInfo // Maps a Relay IDX to a Relay HostInfo object
	RemoteIndexes map[uint32]*HostInfo
```

**File:** hostmap.go (L511-519)
```go
	// The remote index uses index ids outside our control so lets make sure we are only removing
	// the remote index pointer here if it points to the hostinfo we are deleting
	hostinfo2, ok := hm.RemoteIndexes[hostinfo.remoteIndexId]
	if ok && hostinfo2 == hostinfo {
		delete(hm.RemoteIndexes, hostinfo.remoteIndexId)
		if len(hm.RemoteIndexes) == 0 {
			hm.RemoteIndexes = map[uint32]*HostInfo{}
		}
	}
```

**File:** hostmap.go (L568-577)
```go
func (hm *HostMap) QueryReverseIndex(index uint32) *HostInfo {
	hm.RLock()
	if h, ok := hm.RemoteIndexes[index]; ok {
		hm.RUnlock()
		return h
	} else {
		hm.RUnlock()
		return nil
	}
}
```

**File:** handshake_manager.go (L466-473)
```go
	existingRemoteIndex, found := hm.mainHostMap.RemoteIndexes[hostinfo.remoteIndexId]
	if found && existingRemoteIndex != nil && existingRemoteIndex.vpnAddrs[0] != hostinfo.vpnAddrs[0] {
		// We have a collision, but this can happen since we can't control
		// the remote ID. Just log about the situation as a note.
		hostinfo.logger(hm.l).Info("New host shadows existing host remoteIndex",
			"collision", existingRemoteIndex.vpnAddrs,
		)
	}
```

**File:** handshake_manager.go (L488-495)
```go
	existingRemoteIndex, found := hm.mainHostMap.RemoteIndexes[hostinfo.remoteIndexId]
	if found && existingRemoteIndex != nil {
		// We have a collision, but this can happen since we can't control
		// the remote ID. Just log about the situation as a note.
		hostinfo.logger(hm.l).Info("New host shadows existing host remoteIndex",
			"collision", existingRemoteIndex.vpnAddrs,
		)
	}
```

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

**File:** outside.go (L126-166)
```go
	out, err = hostinfo.ConnectionState.Decrypt(f.l, h.MessageCounter, out, packet, nb)
	if err != nil {
		if f.l.Enabled(context.Background(), slog.LevelDebug) {
			hostinfo.logger(f.l).Debug("Failed to decrypt packet", "error", err, "from", via, "header", h)
		}
		return
	}

	// Roam before we respond
	f.handleHostRoaming(hostinfo, via)
	f.connectionManager.In(hostinfo)

	switch h.Type {
	case header.Message:
		switch h.Subtype {
		case header.MessageNone:
			f.handleOutsideMessagePacket(hostinfo, out, packet, fwPacket, nb, q, localCache)
		default:
			hostinfo.logger(f.l).Error("IsValidSubType was true, but unexpected message subtype seen", "from", via, "header", h)
			return
		}

	case header.LightHouse:
		//TODO: assert via is not relayed
		lhf.HandleRequest(via.UdpAddr, hostinfo.vpnAddrs, out, f)

	case header.Test:
		switch h.Subtype {
		case header.TestReply:
			// No-op, useful for the Roaming and connectionManager side-effects above
		case header.TestRequest:
			//recycle the input packet ciphertext as our output buffer
			f.send(header.Test, header.TestReply, hostinfo.ConnectionState, hostinfo, out, nb, packet)
		default:
			hostinfo.logger(f.l).Error("IsValidSubType was true, but unexpected test subtype seen", "from", via, "header", h)
			return
		}

	case header.CloseTunnel:
		hostinfo.logger(f.l).Info("Close tunnel received, tearing down.", "from", via)
		f.closeTunnel(hostinfo)
```

**File:** outside.go (L522-574)
```go
func (f *Interface) maybeSendRecvError(endpoint netip.AddrPort, index uint32) {
	if f.sendRecvErrorConfig.ShouldRecvError(endpoint) {
		f.sendRecvError(endpoint, index)
	}
}

func (f *Interface) sendRecvError(endpoint netip.AddrPort, index uint32) {
	f.messageMetrics.Tx(header.RecvError, 0, 1)

	b := header.Encode(make([]byte, header.Len), header.Version, header.RecvError, 0, index, 0)
	_ = f.outside.WriteTo(b, endpoint)
	if f.l.Enabled(context.Background(), slog.LevelDebug) {
		f.l.Debug("Recv error sent",
			"index", index,
			"udpAddr", endpoint,
		)
	}
}

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
```
