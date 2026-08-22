### Title
Replay-window check/update race in `ConnectionState.Decrypt`/`VerifyRelay` allows duplicate nonce acceptance - (File: `connection_state.go`)

### Summary
The reported Solidity bug is a classic check-then-external-call-then-effect pattern: `_mint` (external call via `safeMint`) executes before the counter that prevents duplicate mints is updated, allowing reentrancy to bypass the counter check. Nebula's data-plane replay-protection code has the same shape: it checks the replay window, performs a "callback-like" external operation (AEAD decryption), and only updates the window state afterward — with the mutex released across that gap. This allows two packets carrying the same `messageCounter` to be processed concurrently and both be accepted, defeating replay/duplicate protection.

### Finding Description
`ConnectionState.Decrypt` and `ConnectionState.VerifyRelay` split the check-then-update sequence into two separate lock/unlock regions with the expensive decrypt operation running in between, unlocked: [1](#0-0) 

Specifically:
1. `cs.decryptLock.Lock(); result := cs.window.Check(...); cs.decryptLock.Unlock()` — verifies the counter hasn't been seen, then releases the lock.
2. `cs.dKey.DecryptDanger(...)` — runs the AEAD decrypt entirely outside the lock.
3. `cs.decryptLock.Lock(); result = cs.window.Update(...); cs.decryptLock.Unlock()` — only now is the window state actually mutated to record the counter as seen.

This mirrors the reported bug class exactly: state is verified, an external/expensive operation runs, and only afterward is the state that guards against duplicates actually committed — with the lock dropped for the entire external-operation window. `VerifyRelay` has the identical structure: `Check` → unlocked `DecryptDanger` → `Update`. [2](#0-1) 

`Decrypt` is reached from the UDP receive path in `readOutsidePackets`, which is invoked per received packet and is the function that resolves the `hostinfo.ConnectionState` for a given tunnel and calls `Decrypt`/`VerifyRelay` on it: [3](#0-2) 

Nebula supports multiple parallel UDP reader "routines" (configurable via `InterfaceConfig.routines`), each independently invoking `readOutsidePackets` for inbound traffic: [4](#0-3) 

Because multiple reader goroutines can each hold a reference to the same `hostinfo`/`ConnectionState` for a given remote peer, two packets carrying an identical `messageCounter` arriving on different queues/goroutines at (near) the same time can both pass `window.Check` before either goroutine reaches `window.Update`, since the lock is released between the two steps and `DecryptDanger` runs unlocked in between.

### Impact Explanation
If both packets have a valid AEAD tag for the same counter value (e.g. a captured/replayed ciphertext, or two copies of the same wire packet delivered via different network paths/duplication), both can pass the replay check and be delivered to the tun device / relay logic. This is a nonce/replay-handling bypass in the authenticated data channel, undermining the guarantee that each `messageCounter` is processed exactly once. Depending on what's replayed, this can result in duplicate packet injection into the tunnel and/or double-processing of relay control messages.

### Likelihood Explanation
Exploitation requires an attacker who can cause duplicate delivery of the same ciphertext to the victim's UDP socket at close to the same time (e.g., network-level duplication/replay, or racing routes/queues), and packets must land on different reader goroutines when `routines > 1` is configured. No CA-signed certificate or established handshake privilege is needed beyond what's needed to have an active tunnel with the target; the race window is the duration of one `DecryptDanger` call, which is small but not zero, and is inherently reachable by anyone able to send duplicate UDP packets to the victim.

### Recommendation
Hold `decryptLock` for the entire check-decrypt-update sequence (or otherwise make check+reserve atomic, e.g. reserve the slot at check time and roll back on decrypt failure) so that no second goroutine can observe the "not yet seen" state for the same counter while a decrypt is in flight. Apply the fix to both `ConnectionState.Decrypt` and `ConnectionState.VerifyRelay`.

### Proof of Concept
Conceptual PoC (race, not deterministic without instrumentation):
1. Establish a tunnel with `routines` > 1 (multiple UDP reader queues) on the victim.
2. Capture one valid encrypted data packet with counter `N` addressed to the victim.
3. Send two copies of that exact packet to the victim's UDP socket in quick succession so that (with multi-queue/SO_REUSEPORT-style dispatch) they are picked up by two different reader goroutines calling `readOutsidePackets` → `ConnectionState.Decrypt` concurrently for the same `hostinfo`.
4. Both goroutines call `cs.window.Check(l, N)` before either calls `cs.window.Update(l, N)` (since the lock is released between the two calls and `DecryptDanger` runs unlocked), so both may pass the check and have the same counter accepted twice instead of the second being rejected with `ErrAlreadySeen`. [1](#0-0)

### Citations

**File:** connection_state.go (L61-82)
```go
func (cs *ConnectionState) Decrypt(l *slog.Logger, messageCounter uint64, out []byte, packet []byte, nb []byte) ([]byte, error) {
	var err error
	cs.decryptLock.Lock()
	result := cs.window.Check(l, messageCounter)
	cs.decryptLock.Unlock()
	if !result {
		return nil, ErrAlreadySeen
	}

	out, err = cs.dKey.DecryptDanger(out, packet[:header.Len], packet[header.Len:], messageCounter, nb)
	if err != nil {
		return nil, err
	}

	cs.decryptLock.Lock()
	result = cs.window.Update(l, messageCounter)
	cs.decryptLock.Unlock()
	if !result {
		return nil, ErrAlreadySeen
	}
	return out, nil
}
```

**File:** connection_state.go (L84-108)
```go
// VerifyRelay verifies AEAD protected (but not encrypted) relay frames. packet must be length-checked by the caller.
func (cs *ConnectionState) VerifyRelay(l *slog.Logger, messageCounter uint64, packet []byte, nb []byte) error {
	cs.decryptLock.Lock()
	result := cs.window.Check(l, messageCounter)
	cs.decryptLock.Unlock()
	if !result {
		return ErrAlreadySeen
	}

	signedPayload := packet[:len(packet)-cs.dKey.Overhead()]
	signatureValue := packet[len(packet)-cs.dKey.Overhead():]
	_, err := cs.dKey.DecryptDanger(nil, signedPayload, signatureValue, messageCounter, nb)
	if err != nil {
		return err
	}

	cs.decryptLock.Lock()
	result = cs.window.Update(l, messageCounter)
	cs.decryptLock.Unlock()
	if !result {
		return ErrAlreadySeen
	}

	return nil
}
```

**File:** outside.go (L89-120)
```go
	var hostinfo *HostInfo
	if isMessageRelay {
		hostinfo = f.hostMap.QueryRelayIndex(h.RemoteIndex)
	} else {
		hostinfo = f.hostMap.QueryIndex(h.RemoteIndex)
	}

	// At this point we should have a valid existing tunnel, verify and send
	// recvError if necessary
	if hostinfo == nil || hostinfo.ConnectionState == nil {
		if !via.IsRelayed {
			f.maybeSendRecvError(via.UdpAddr, h.RemoteIndex)
		}
		return
	}

	if len(packet) < header.Len+hostinfo.ConnectionState.dKey.Overhead() {
		f.messageMetrics.RxInvalid(1)
		if f.l.Enabled(context.Background(), slog.LevelDebug) {
			f.l.Debug("packet too small", "from", via, "length", len(packet))
		}
		return
	}

	// All remaining packets are encrypted
	if isMessageRelay {
		// Relay packets are special, this branch should always early-return
		if err = hostinfo.ConnectionState.VerifyRelay(f.l, h.MessageCounter, packet, nb); err != nil {
			if f.l.Enabled(context.Background(), slog.LevelDebug) {
				hostinfo.logger(f.l).Debug("Failed to verify relay packet", "error", err, "from", via, "header", h)
			}
			return
```

**File:** interface.go (L28-53)
```go
type InterfaceConfig struct {
	HostMap            *HostMap
	Outside            udp.Conn
	Inside             overlay.Device
	pki                *PKI
	Cipher             string
	Firewall           *Firewall
	DnsServer          *dnsServer
	HandshakeManager   *HandshakeManager
	lightHouse         *LightHouse
	connectionManager  *connectionManager
	DropLocalBroadcast bool
	DropMulticast      bool
	routines           int
	MessageMetrics     *MessageMetrics
	version            string
	relayManager       *relayManager
	punchy             *Punchy

	tryPromoteEvery uint32
	reQueryEvery    uint32
	reQueryWait     time.Duration

	ConntrackCacheTimeout time.Duration
	l                     *slog.Logger
}
```
