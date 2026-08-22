### Title
Unauthenticated `header.RecvError` packet allows spoofed teardown of an established tunnel - (File: outside.go)

### Finding Description
`readOutsidePackets` dispatches `header.RecvError` packets to `f.handleRecvError(via.UdpAddr, h)` before any AEAD/Noise verification occurs, since `RecvError` is handled in the "Unencrypted packets" switch alongside `header.Handshake` [1](#0-0) . `handleRecvError` looks up the `HostInfo` via `f.hostMap.QueryIndex(h.RemoteIndex)` and then compares the packet's claimed remote (`hostinfo.GetRemote()`) against the actual UDP source address (`addr`); only when the stored remote `IsValid()` and differs from `addr` is the packet ignored — otherwise it proceeds to call `f.closeTunnel(hostinfo)` and `f.handshakeManager.DeleteHostInfo(hostinfo)`. There is no cryptographic authentication (no HMAC/AEAD/certificate check) tying the `RecvError` packet to the session — the only "authentication" is (1) knowing/guessing the 32-bit `h.RemoteIndex` and (2) the source IP:port matching what the host currently believes is its peer's remote address. Because UDP source addresses can be spoofed on many network paths, and `RemoteIndex` is a locally-chosen value with no cryptographic binding to the session, an attacker satisfying both conditions can force teardown of a legitimate, already-authenticated tunnel without ever presenting valid Noise/cert material.

### Impact Explanation
This is a remote, unauthenticated denial-of-service against an established Nebula tunnel: `closeTunnel` + `DeleteHostInfo` destroy the session state, forcing both peers to redo a full handshake, disrupting connectivity for legitimate traffic. This maps to Nebula's "remote crash/wedge" or "remote state poisoning" bounty impact category, scoped strictly to tunnel-teardown DoS (not decryption/forgery/auth bypass of data traffic).

### Likelihood Explanation
Exploitability depends entirely on two independent preconditions that are non-trivial in a realistic deployment: (1) the attacker must spoof a UDP source IP:port that exactly matches the current `hostinfo.remote` of the target's peer — most modern networks/ISPs perform BCP38 egress filtering that blocks arbitrary source-IP spoofing, and (2) the attacker must know or brute-force the 32-bit `RemoteIndex` for that specific hostinfo, which is not transmitted in cleartext anywhere an off-path attacker could observe it without already being able to see the encrypted traffic (in which case address-matching is no longer meaningfully "spoofed"). Under the audit's stated precondition that "via.UdpAddr spoofable" and RemoteIndex is "guessed/observed," the code path is real and unguarded by any crypto check, but real-world likelihood is low-to-moderate and highly dependent on network position (on-path or spoof-capable attacker) rather than a pure blind-remote attacker.

### Recommendation
Do not allow `RecvError` to trigger `closeTunnel`/`DeleteHostInfo` based solely on `RemoteIndex` + source-address match. Require the message to be authenticated (e.g., HMAC using the session key, or requiring the current AEAD keys to verify a short authenticated payload) before tearing down a hostinfo that already has an established `ConnectionState`. At minimum, rate-limit/backoff repeated `RecvError`-triggered teardowns per hostinfo and log/alert on mismatched-address attempts to detect spoofing attempts.

### Proof of Concept
Unit test in `outside_test.go` (or new test file):
1. Establish two `HostInfo` objects with a valid `ConnectionState` and a known `RemoteIndex`, set `hostinfo.remote` to a specific `netip.AddrPort` (e.g. `1.2.3.4:4242`).
2. Construct a `header.H` with `Type = header.RecvError` and `RemoteIndex` equal to the established hostinfo's local index.
3. Call `f.handleRecvError(spoofedAddr, h)` where `spoofedAddr == hostinfo.remote` (simulating a spoofed source matching the legitimate remote).
4. Assert that `f.hostMap.QueryIndex(index)` returns `nil` afterward (i.e., `closeTunnel`/`DeleteHostInfo` executed) even though no handshake/Noise material was ever presented by the caller.
5. As a control, repeat with `spoofedAddr != hostinfo.remote` and assert the hostinfo remains intact, confirming the address-equality check is the only gate.
6. Optional fuzz: measure entropy/guessability of `RemoteIndex` allocation (`f.hostMap` index generator) to quantify brute-force feasibility for an off-path attacker.

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
