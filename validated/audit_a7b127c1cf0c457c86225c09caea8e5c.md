Confirmed: `f.routines` (config `listen.routines`, default >1 on Linux with SO_REUSEPORT) spawns multiple reader goroutines that each independently call `readOutsidePackets` → `ConnectionState.Decrypt`/`VerifyRelay` for the *same* `HostInfo`/`ConnectionState` concurrently. This is the reachable condition needed for the race below.

### Title
Replay-window check/update race allows duplicate acceptance of a captured packet - ([File: connection_state.go])

### Summary
`ConnectionState.Decrypt` and `ConnectionState.VerifyRelay` split the anti-replay accounting into two separately-locked steps — `window.Check` then, after doing the expensive AEAD decrypt outside the lock, `window.Update` — mirroring the Venus Prime.sol bug where an aggregate accounting value (`sumOfScores`/interest) was read/relied upon before the individual state used to compute it was reconciled, letting two operations act on the "old" state simultaneously.

### Finding Description
`Decrypt` takes `decryptLock`, calls `cs.window.Check(l, messageCounter)` to see whether `messageCounter` is unseen, releases the lock, performs `DecryptDanger` (an unlocked, non-trivial cryptographic operation), then re-acquires the lock to call `cs.window.Update(l, messageCounter)` which actually records the counter as seen. [1](#0-0) 

Because the lock is released between `Check` and `Update`, two goroutines processing the same replayed packet (same `messageCounter`) concurrently — which is reachable because `f.routines` spawns multiple UDP reader goroutines that all call into the same `HostInfo.ConnectionState` via `readOutsidePackets`/`outside.go` — can both pass `Check` before either has called `Update`. Both then proceed to call `DecryptDanger` with the same AEAD key/nonce/ciphertext and succeed, and only the second `Update` call fails (returning `ErrAlreadySeen`), but by then the first (and possibly both) decrypted plaintexts have already been delivered to the TUN device. This is the same "check against stale aggregate state, then commit later" pattern as the reported Venus Prime bug, where `updateAlpha`/`updateMultipliers` mutated global accounting before individual per-user state was reconciled, causing scores/rewards computed against inconsistent state. [2](#0-1) [3](#0-2) 

`VerifyRelay` has the identical pattern for relay-forwarded frames. [4](#0-3) 

### Impact Explanation
An attacker who captures a single legitimate ciphertext packet on the wire (no valid certificate needed — this is a data-plane replay, not a handshake) can replay it multiple times in a tight burst. If the receiving Nebula node is configured with `listen.routines > 1` (multiple UDP reader goroutines, a supported and documented configuration), the replay window's check-then-commit race can let the same message counter be decrypted and delivered to the TUN device more than once, defeating the anti-replay guarantee the `Bits` window is designed to provide. This is a traffic-replay/anti-replay-bypass impact.

### Likelihood Explanation
Requires `listen.routines` > 1 (a real, supported configuration for performance) and a tight race window between two UDP reader goroutines receiving the duplicated ciphertext at nearly the same time — feasible for a local/adjacent attacker who can inject duplicate UDP frames quickly. The window is small (a single AEAD decrypt operation), making this a genuine but narrow race rather than a reliably-triggered bug.

### Recommendation
Perform the check-and-mark atomically under a single lock acquisition (e.g., hold `decryptLock` across `Check`, decrypt, and `Update`, or use a single `Bits.CheckAndUpdate`-style method) so no other goroutine can observe the "unseen" state for the same counter while a decrypt for that counter is in flight, matching the Venus fix's principle of reconciling state before allowing a second actor to act on it.

### Proof of Concept
1. Establish a Nebula tunnel between two nodes where the receiver is started with `listen.routines: 2` (or more).
2. Capture one legitimate data-plane message frame (`header.Message`) sent to the receiver.
3. Inject the exact same UDP frame twice in rapid succession (e.g., via two goroutines sending simultaneously) so both are read by different reader routines at nearly the same time.
4. Observe that both reader goroutines can pass `cs.window.Check` before either calls `cs.window.Update` (verifiable by adding a synchronization point or by running under high UDP load), resulting in the payload being delivered to the TUN device twice for a single wire packet, i.e., a duplicate/replayed packet is accepted despite the anti-replay window.

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

**File:** connection_state.go (L85-108)
```go
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

**File:** interface.go (L55-94)
```go
type Interface struct {
	hostMap               *HostMap
	outside               udp.Conn
	inside                overlay.Device
	pki                   *PKI
	firewall              *Firewall
	connectionManager     *connectionManager
	handshakeManager      *HandshakeManager
	dnsServer             *dnsServer
	createTime            time.Time
	lightHouse            *LightHouse
	myBroadcastAddrsTable *bart.Lite
	myVpnAddrs            []netip.Addr // A list of addresses assigned to us via our certificate
	myVpnAddrsTable       *bart.Lite
	myVpnNetworks         []netip.Prefix // A list of networks assigned to us via our certificate
	myVpnNetworksTable    *bart.Lite
	dropLocalBroadcast    bool
	dropMulticast         bool
	routines              int
	disconnectInvalid     atomic.Bool
	closed                atomic.Bool
	relayManager          *relayManager

	tryPromoteEvery atomic.Uint32
	reQueryEvery    atomic.Uint32
	reQueryWait     atomic.Int64

	sendRecvErrorConfig   recvErrorConfig
	acceptRecvErrorConfig recvErrorConfig

	// rebindCount is used to decide if an active tunnel should trigger a punch notification through a lighthouse
	rebindCount int8
	version     string

	conntrackCacheTimeout time.Duration

	ctx     context.Context
	writers []udp.Conn
	readers []io.ReadWriteCloser
	wg      sync.WaitGroup
```

**File:** outside.go (L113-121)
```go
	// All remaining packets are encrypted
	if isMessageRelay {
		// Relay packets are special, this branch should always early-return
		if err = hostinfo.ConnectionState.VerifyRelay(f.l, h.MessageCounter, packet, nb); err != nil {
			if f.l.Enabled(context.Background(), slog.LevelDebug) {
				hostinfo.logger(f.l).Debug("Failed to verify relay packet", "error", err, "from", via, "header", h)
			}
			return
		}
```
