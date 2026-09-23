No genuine analog exists in this Go standard library repository. The kernel CVE is specific to the Linux DCP crypto driver's misuse of `sg_init_one()` on a `vmalloc`-backed goroutine/stack buffer under `CONFIG_VMAP_STACK=y` — a kernel scatterlist/DMA-mapping primitive that has no equivalent construct in Go. Go's cgo boundary explicitly forbids passing stack-resident Go memory across the C boundary in ways that would reproduce this bug class: the cgo pointer-passing rules documented in `src/cmd/cgo/doc.go` prohibit C code from retaining Go pointers, and calls into C run with the goroutine switched to a fixed system stack (see the `cgocallback`/`asmcgocall` transition code in `src/runtime/asm_amd64.s`), which is precisely the mechanism that prevents the "stack moved out from under an in-flight DMA/scatter-gather buffer" bug from having a Go analog. [1](#0-0) [2](#0-1) 

Go's own crypto internals (`crypto/internal/boring`, `crypto/internal/fips140/aes/gcm`) pass slices/buffers to assembly or cgo-wrapped BoringSSL routines, but these are ordinary heap/stack Go values subject to the standard cgo pointer rules and goroutine-stack-copy safety guarantees — not scatterlist/DMA structures that can be invalidated by a `vmalloc` vs. physically-contiguous memory distinction, which is a kernel-only concept. [3](#0-2) 

I did not find a reachable, unauthenticated-input code path in this repository that reproduces the specific "improper scatter-gather use with vmap'd stack" primitive. Per the reporting rules, stretching this into an analogy would not be a real finding.

### No Vulnerability found for this question.

### Citations

**File:** src/cmd/cgo/doc.go (L395-413)
```go
This implies that C code may not keep a copy of a string, slice,
channel, and so forth, because they cannot be pinned with
[runtime.Pinner].

The _GoString_ type also may not be pinned with [runtime.Pinner].
Because it includes a Go pointer, the memory it points to is only pinned
for the duration of the call; _GoString_ values may not be retained by C
code.

A Go function called by C code may return a Go pointer to pinned memory
(which implies that it may not return a string, slice, channel, and so
forth). A Go function called by C code may take C pointers as arguments,
and it may store non-pointer data, C pointers, or Go pointers to pinned
memory through those pointers. It may not store a Go pointer to unpinned
memory in memory pointed to by a C pointer (which again, implies that it
may not store a string, slice, channel, and so forth). A Go function
called by C code may take a Go pointer but it must preserve the property
that the Go memory to which it points (and the Go memory to which that
memory points, and so on) is pinned.
```

**File:** src/runtime/asm_amd64.s (L1012-1046)
```text
// func cgocallback(fn, frame unsafe.Pointer, ctxt uintptr)
// See cgocall.go for more details.
TEXT ·cgocallback(SB),NOSPLIT,$24-24
	NO_LOCAL_POINTERS

	// Skip cgocallbackg, just dropm when fn is nil, and frame is the saved g.
	// It is used to dropm while thread is exiting.
	MOVQ	fn+0(FP), AX
	CMPQ	AX, $0
	JNE	loadg
	// Restore the g from frame.
	get_tls(CX)
	MOVQ	frame+8(FP), BX
	MOVQ	BX, g(CX)
	JMP	dropm

loadg:
	// If g is nil, Go did not create the current thread,
	// or if this thread never called into Go on pthread platforms.
	// Call needm to obtain one m for temporary use.
	// In this case, we're running on the thread stack, so there's
	// lots of space, but the linker doesn't know. Hide the call from
	// the linker analysis by using an indirect call through AX.
	get_tls(CX)
#ifdef GOOS_windows
	MOVL	$0, BX
	CMPQ	CX, $0
	JEQ	2(PC)
#endif
	MOVQ	g(CX), BX
	CMPQ	BX, $0
	JEQ	needm
	MOVQ	g_m(BX), BX
	MOVQ	BX, savedm-8(SP)	// saved copy of oldm
	JMP	havem
```

**File:** src/crypto/internal/boring/boring.go (L1-33)
```go
// Copyright 2017 The Go Authors. All rights reserved.
// Use of this source code is governed by a BSD-style
// license that can be found in the LICENSE file.

//go:build boringcrypto && linux && (amd64 || arm64) && !android && !msan

package boring

/*
// goboringcrypto_linux_amd64.syso references pthread functions.
#cgo LDFLAGS: "-pthread"

#include "goboringcrypto.h"
*/
import "C"
import (
	"crypto/internal/boring/sig"
	_ "crypto/internal/boring/syso"
	"crypto/internal/fips140"
	"internal/stringslite"
	"math/bits"
	"unsafe"
)

const available = true

func init() {
	C._goboringcrypto_BORINGSSL_bcm_power_on_self_test()
	if C._goboringcrypto_FIPS_mode() != 1 {
		panic("boringcrypto: not in FIPS mode")
	}
	sig.BoringCrypto()
}
```
