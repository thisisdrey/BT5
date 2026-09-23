## Finding

### Title
Crafted IPv4 header causes negative byte count returned by `stripIPv4Header`, leading to slice-bounds panic in `net.IPConn` readers - (File: src/net/iprawsock_posix.go)

### Summary
`stripIPv4Header` validates that the IHL-derived header length `l` fits within the *buffer* capacity (`len(b)`) but never checks it against the *actual number of bytes received on the wire* (`n`). A remote peer sending a raw IP packet with a header-length field larger than the packet actually delivered (but still ≤ buffer size) causes the function to return a negative byte count from `IPConn.ReadFrom`/`ReadFromIP`, which is the same "crafted IP header length field consumed without validating actual payload size" bug class as CVE-2018-16601's `prvProcessIPPacket`.

### Finding Description
An unprivileged remote attacker sends a crafted raw IPv4 datagram to a Go program that reads packets with `net.ListenIP`/`net.DialIP` and `IPConn.ReadFrom`/`ReadFromIP`. On POSIX, `(*IPConn).readFrom` calls the kernel `readFrom`, obtaining `n` (actual bytes received) and the raw buffer `b`, then calls `stripIPv4Header(n, b)`: [1](#0-0) 

```go
func stripIPv4Header(n int, b []byte) int {
	if len(b) < 20 {
		return n
	}
	l := int(b[0]&0x0f) << 2
	if 20 > l || l > len(b) {
		return n
	}
	if b[0]>>4 != 4 {
		return n
	}
	copy(b, b[l:])
	return n - l
}
```

The IHL nibble `b[0]&0x0f` is attacker-controlled (part of the raw IP header the kernel delivers unmodified for raw sockets). The bounds check `l > len(b)` only guards against the header length exceeding the *buffer capacity*, not the *actually-received* byte count `n`. If the attacker sends a short/truncated IP packet (small `n`) while setting IHL to indicate a large header (e.g., 60 bytes, the max), and the caller's read buffer is comfortably larger (as is typical, e.g. 1500+ bytes), the check `l > len(b)` passes even though `l > n`. The function then returns `n - l`, a negative integer, as the byte count from `ReadFrom`/`ReadFromIP`.

Callers of `ReadFrom`/`ReadFromIP` universally follow the standard Go idiom `data := buf[:n]` to slice the buffer to the received length. A negative `n` makes this slice expression panic with "slice bounds out of range", crashing the victim's read loop/goroutine.

### Impact Explanation
This is a remotely triggerable denial-of-service: an unauthenticated network peer that can send a crafted IP packet to a raw-socket listener can force a Go process to panic when it slices its receive buffer using the returned (negative) count. This is analogous in root cause (header-length field trusted beyond the actually-received data, driving an out-of-bounds/negative-length operation) to CVE-2018-16601. Under Go's vulnerability handling, this would be assessed as a PUBLIC-track issue (a parser/library function returning a value that reliably causes a panic in normal caller code on malicious network input), not a memory-safety RCE, since Go's slice-bounds check converts the flaw into a safe panic rather than memory corruption.

### Likelihood Explanation
Reachable when a Go program uses `net.IPConn.ReadFrom`/`ReadFromIP` on an "ip"/"ip4" raw socket — a normal, documented workflow (e.g., custom ICMP/IP tooling) — and passes the returned count to slice its buffer, which is the standard and expected usage pattern shown in the package's own examples. The attacker only needs the ability to send an IP packet to the listening host; no authentication or privileged position is required. Note the existing package doc already discloses raw reads may be incomplete (`BUG(mikio)` comment) but this note does not warn about negative counts.

### Recommendation
Bound the header length check against the number of bytes actually received, not merely the buffer capacity, e.g. change `l > len(b)` to also require `l <= n`, and return `n, false` (or similarly avoid subtracting) when the declared header length exceeds the bytes actually read.

### Proof of Concept
```go
package net

import "testing"

func TestStripIPv4HeaderNegative(t *testing.T) {
	// Simulate a truncated raw IPv4 read: kernel delivered only 30 bytes (n=30),
	// but attacker set IHL=15 (60-byte header) in the first byte, and the
	// caller's buffer (b) is much larger than what was actually received.
	b := make([]byte, 1500)
	b[0] = 0x4f // version 4, IHL = 15 -> 60-byte header
	n := 30

	got := stripIPv4Header(n, b)
	if got < 0 {
		t.Fatalf("stripIPv4Header returned negative count %d; callers doing buf[:n] will panic", got)
	}
}
```
Expected (buggy) behavior: `got == -30`, demonstrating that a crafted header causes a negative byte count to be returned, which will panic any caller performing `buf[:n]` on the result of `ReadFrom`/`ReadFromIP`.

### Citations

**File:** src/net/iprawsock_posix.go (L45-73)
```go
func (c *IPConn) readFrom(b []byte) (int, *IPAddr, error) {
	// TODO(cw,rsc): consider using readv if we know the family
	// type to avoid the header trim/copy
	var addr *IPAddr
	n, sa, err := c.fd.readFrom(b)
	switch sa := sa.(type) {
	case *syscall.SockaddrInet4:
		addr = &IPAddr{IP: sa.Addr[0:]}
		n = stripIPv4Header(n, b)
	case *syscall.SockaddrInet6:
		addr = &IPAddr{IP: sa.Addr[0:], Zone: zoneCache.name(int(sa.ZoneId))}
	}
	return n, addr, err
}

func stripIPv4Header(n int, b []byte) int {
	if len(b) < 20 {
		return n
	}
	l := int(b[0]&0x0f) << 2
	if 20 > l || l > len(b) {
		return n
	}
	if b[0]>>4 != 4 {
		return n
	}
	copy(b, b[l:])
	return n - l
}
```
