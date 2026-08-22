### Title
`createICMPv6EchoResponse` slices `out[:len(packet)]` without a `cap(out)` bounds check, unlike sibling reject-packet builders - ([File: iputil/packet.go])

### Summary
`createICMPv6EchoResponse` (and its IPv4 counterpart `createICMPv4EchoResponse`) blindly execute `out = out[:len(packet)]` after only validating `len(packet)`, with no check that `cap(out) >= len(packet)`. Every other packet-building function in the same file (`ipv4CreateRejectICMPPacket`, `ipv4CreateRejectTCPPacket`, `ipv6CreateRejectICMPPacket`, `ipv6CreateRejectTCPPacket`) explicitly guards with `if outLen > cap(out) { return nil }` before reslicing, showing this check is the established invariant that was omitted here.

### Finding Description
`createICMPv6EchoResponse` at [1](#0-0)  validates `len(packet) < ipv6.HeaderLen+8`, `len(packet) > 9001`, the Next Header byte, and the ICMPv6 type, but never checks `cap(out)` before doing `out = out[:len(packet)]`. Contrast this with the reject-packet builders, e.g. [2](#0-1)  and [3](#0-2) , which all perform `if outLen > cap(out) { return nil }` immediately before slicing `out`. `createICMPv4EchoResponse` has the identical omission at [4](#0-3) .

If a caller passes an `out` buffer whose capacity is smaller than the incoming `packet` length (e.g. a caller that sizes `out` based on a fixed reply size rather than the attacker-controlled request size), `out[:len(packet)]` panics with "slice bounds out of range". The packet length is entirely attacker-controlled up to 9001 bytes, so any caller supplying an undersized `out` buffer is vulnerable to a crash triggered purely by tun-routed local IPv6 ICMP Echo Request traffic — no certificate, handshake, or authentication is required to reach this code, since it operates on the raw decapsulated packet before any Nebula-level trust check occurs.

However, I was not able to fully confirm from the available index whether the only real caller (`CreateICMPEchoResponse`, invoked in the tun packet-handling path) always allocates `out` with `cap(out) >= 9001` or MTU-sized buffers in the actual runtime call sites (e.g. `overlay/tun_disabled.go` or the firewall/inside packet handling code path was not fully retrievable). If the caller always provides a buffer with sufficient capacity (matching `MaxRejectPacketSize`/MTU), the missing check is dead code from a robustness standpoint but not remotely triggerable.

### Impact Explanation
If reachable with an undersized buffer, this causes a remote crash (panic) of the Nebula process handling tun-routed ICMPv6 echo requests, which is a real availability impact (remote crash/wedge) under the bounty rules. However, this requires an internal precondition (caller-supplied `out` capacity smaller than `len(packet)`) that could not be confirmed against the current call sites within available context.

### Likelihood Explanation
Exploitability depends entirely on whether existing callers of `CreateICMPEchoResponse`/`createICMPv6EchoResponse` allocate a fixed/small `out` buffer while `packet` length is attacker-controlled up to 9001 bytes. I could not verify the actual buffer-sizing at the call site(s) in `overlay/tun_disabled.go` or other integration points within the tool budget available, so I cannot confirm this is reachable with the caller's real buffer sizes rather than being purely a defensive-gap without exploitable impact today.

### Recommendation
Add the same guard used elsewhere in this file: `if len(packet) > cap(out) { return nil }` before `out = out[:len(packet)]` in both `createICMPv4EchoResponse` and `createICMPv6EchoResponse`, to make the invariant "untrusted length never drives an out-of-bounds reslice" hold uniformly across all packet builders in `iputil/packet.go`.

### Proof of Concept
```go
func TestCreateICMPv6EchoResponse_UndersizedOutPanics(t *testing.T) {
    packetLen := ipv6.HeaderLen + 8 // minimum valid echo request
    packet := make([]byte, packetLen)
    packet[0] = 0x60 // version 6
    packet[6] = 58    // next header ICMPv6
    packet[ipv6.HeaderLen] = 128 // echo request

    // out has smaller capacity than packet
    out := make([]byte, 0, packetLen-1)

    defer func() {
        if r := recover(); r != nil {
            t.Fatalf("createICMPv6EchoResponse panicked: %v", r)
        }
    }()

    result := createICMPv6EchoResponse(packet, out)
    // Expect a nil/rejection instead of a panic
    if result != nil {
        t.Fatalf("expected nil due to insufficient out capacity, got result")
    }
}
```
Expected (desired) behavior: the function returns `nil` when `cap(out) < len(packet)`. Actual current behavior: `out[:len(packet)]` panics with "slice bounds out of range" because no capacity check precedes the reslice.

### Citations

**File:** iputil/packet.go (L227-232)
```go
	outLen := ipv6.HeaderLen + 8 + packetLen
	if outLen > cap(out) {
		return nil
	}

	out = out[:outLen]
```

**File:** iputil/packet.go (L277-282)
```go
	outLen := ipv6.HeaderLen + tcpLen
	if outLen > cap(out) {
		return nil
	}

	out = out[:outLen]
```

**File:** iputil/packet.go (L387-399)
```go
func createICMPv4EchoResponse(packet, out []byte) []byte {
	// Return early if this is not a simple ICMP Echo Request
	//TODO: make constants out of these
	if !(len(packet) >= 28 && len(packet) <= 9001 && packet[0] == 0x45 && packet[9] == 0x01 && packet[20] == 0x08) {
		return nil
	}

	// We don't support fragmented packets
	if packet[7] != 0 || (packet[6]&0x2F != 0) {
		return nil
	}

	out = out[:len(packet)]
```

**File:** iputil/packet.go (L421-438)
```go
func createICMPv6EchoResponse(packet, out []byte) []byte {
	// IPv6 header (40 bytes) + ICMPv6 header (8 bytes minimum)
	if len(packet) < ipv6.HeaderLen+8 || len(packet) > 9001 {
		return nil
	}

	// Next Header must be ICMPv6 (58)
	if packet[6] != 58 {
		return nil
	}

	// ICMPv6 type must be Echo Request (128)
	if packet[ipv6.HeaderLen] != 128 {
		return nil
	}

	out = out[:len(packet)]
	copy(out, packet)
```
