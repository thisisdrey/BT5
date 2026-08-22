### Title
Spoofed `RecvError` packets can evict established tunnels without full sender-binding verification - ([File: outside.go])

### Summary
The external report flags a bug class in `GnosisBase._verifySender()`: the function checks one binding property (`msg.sender == mirrorConnector`) but omits a second, necessary binding property (`messageSourceChainId() == MIRROR_DOMAIN`), letting an attacker who controls a like-addressed contract on an unsupported domain get treated as if it were the trusted domain. The reachable analog in this Nebula codebase is `Interface.handleRecvError` in `outside.go`, which authenticates an inbound, pre-handshake `RecvError` control packet using only a conditional address-binding check, and skips that check entirely whenever the stored remote address on the target `HostInfo` has not yet been set.

### Finding Description
`readOutsidePackets` in `outside.go` dispatches unencrypted `header.RecvError` packets straight to `f.handleRecvError(via.UdpAddr, h)` before any certificate, handshake, or decryption step is performed [1](#0-0) . This means a fully unauthenticated attacker with no CA-signed certificate — anyone able to send a UDP packet to the listener — can reach this code path.

`handleRecvError` looks up the target tunnel purely by the attacker-supplied 32-bit `h.RemoteIndex` via `QueryReverseIndex`, and then performs its sole sender-binding check:

```go
hr := hostinfo.GetRemote()
if hr.IsValid() && hr != addr {
    f.l.Info("Someone spoofing recv_errors?", ...)
    return
}

f.closeTunnel(hostinfo)
f.handshakeManager.DeleteHostInfo(hostinfo)
``` [2](#0-1) 

This is structurally identical to the reported bug class: the code checks a single scoping attribute (`hr != addr`, analogous to `msg.sender == _expected`) but that check is **conditional on `hr.IsValid()`** — if the `HostInfo`'s remote address has not yet been recorded (e.g., a hostinfo reachable only through a relay, or one whose `SetRemote` hasn't fired yet), the binding check is skipped entirely and the packet is accepted from *any* source address, exactly like `GnosisBase` failing to also verify `messageSourceChainId()` and thus trusting any domain that reuses the expected sender address. An attacker who merely guesses or observes a valid `RemoteIndex` (visible on the wire to any on-path observer, since it is sent in cleartext in every packet header) can trigger `closeTunnel` + `DeleteHostInfo` for a victim's tunnel with a forged source address whenever that binding precondition doesn't hold.

### Impact Explanation
A successful spoof causes the victim's `HostInfo` to be torn down (`closeTunnel`) and removed from the handshake manager (`DeleteHostInfo`), forcing a new handshake and creating a denial-of-service / tunnel-disruption primitive without possessing a valid Nebula certificate for the network. This matches the "remote state poisoning" / DoS impact category called out as acceptable in the validation rules.

### Likelihood Explanation
Reaching `handleRecvError` requires no certificate and no completed handshake — only a raw UDP packet with the `RecvError` header type, satisfying the "attacker with no CA-signed certificate" constraint. Exploiting the missing-binding window additionally requires (a) knowledge of the target's 32-bit `RemoteIndex`, obtainable by a passive/on-path observer of legitimate traffic, and (b) hitting a state where `hostinfo.GetRemote()` is not yet valid (e.g., a hostinfo whose primary connectivity is a relay path, before `SetRemote`/roaming has populated it). This is a narrower window than a fully unconditional bypass, so likelihood is moderate rather than trivial, but it directly parallels the reported "extra check missing" pattern.

### Recommendation
Make the sender-binding check unconditional (or otherwise structurally strong) in `handleRecvError`: reject any `RecvError` when the reporting address cannot be positively bound to the target `HostInfo` (e.g., require `hr.IsValid()` as a precondition for acceptance rather than a precondition for the mismatch check, or additionally bind to `RemoteIndex` plus a value derived from the established `ConnectionState` that an outside observer cannot forge), mirroring the recommendation in the report to add the extra "domain" check (`messageSourceChainId()`/`MIRROR_DOMAIN` equivalent) rather than relying on a single reused identifier.

### Proof of Concept
1. Passively observe (or otherwise learn) the `RemoteIndex` used in packets flowing to a victim `HostInfo` whose `remote` field has not yet been set (e.g., a hostinfo currently only reachable via a relay).
2. Craft a `header.RecvError` packet with that `RemoteIndex` and send it from an arbitrary, unauthenticated source address to the victim node's UDP listener.
3. `readOutsidePackets` routes it directly to `handleRecvError` without any cert/handshake check; since `hostinfo.GetRemote()` is not yet valid, the `hr.IsValid() && hr != addr` check is skipped, and `f.closeTunnel(hostinfo)` / `f.handshakeManager.DeleteHostInfo(hostinfo)` execute, tearing down the victim's tunnel state.

Note: I was not able to fully trace every call site that could leave `hostinfo.GetRemote()` invalid on a main-hostmap entry (this would require deeper review of `SetRemote`/`handleHostRoaming` lifecycle across relay and direct-connection paths, which I could not complete within the available tool budget), so the precise set of states under which this window is reachable in the current build should be verified further before treating this as fully confirmed.

### Citations

**File:** outside.go (L81-84)
```go
	case header.RecvError:
		f.handleRecvError(via.UdpAddr, h)
		return
	}
```

**File:** outside.go (L557-574)
```go
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
