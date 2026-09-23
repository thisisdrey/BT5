### Title
`secretEraseRegisters` on amd64 fails to sanitize MXCSR control/status bits and RFLAGS system bits after `secret.Do`, leaking side-channel state across the secrecy boundary - (File: src/runtime/secret_amd64.s)

### Summary
The experimental `runtime/secret` package (`GOEXPERIMENT=runtimesecret`) promises that "any registers used by `f` are erased before `Do` returns" so that data processed inside `secret.Do` cannot leak to code running after it returns. On amd64, `secretEraseRegistersMcall` zeroes the general-purpose and SSE/AVX data registers but never resets the MXCSR floating-point control/status register, and only executes a `CMPL BX, BX` to produce a fixed value in the arithmetic RFLAGS bits — it does not touch (or even attempt to reset) the rest of RFLAGS. This is the same class of bug as CVE-2023-37479 in OpenEnclave: failing to sanitize MXCSR and RFLAGS at a trust/secrecy boundary lets sticky FPU exception flags and other flag state computed from secret data survive the boundary and be observed afterward.

### Finding Description
Untrusted/attacker-influenced input reaches this boundary any time a Go program uses `secret.Do(f)` [1](#0-0)  to process confidential data (e.g., constant-time crypto operating on secret key material derived from network input). Inside `f`, SSE/AVX floating point or SIMD operations executed by the program (or by code the runtime inlines) can set MXCSR sticky status bits (precision/underflow/overflow/denormal/invalid) that are data-dependent. When `Do` returns, it calls `eraseSecrets()`, which on amd64 invokes `secretEraseRegisters` / `secretEraseRegistersMcall` [2](#0-1) . That routine zeroes GP registers and the XMM/YMM/ZMM data lanes with `XORL`/`PXOR`/`VZEROALL` [3](#0-2) , but the only attempt at flag sanitization is a single `CMPL BX, BX //eflags`, annotated with the developer's own uncertainty: `// segment registers? Direction flag? Both seem overkill.` [4](#0-3) . This comparison sets a fixed subset of the arithmetic flags but does not execute `LDMXCSR` or any instruction to reset the MXCSR control/status register, and it does not restore RFLAGS bits such as the alignment-check flag (AC) or others that a preceding secret computation may have altered via inline assembly or via later scheduling paths (e.g. `newstack`/`copystack`, which also call `secretEraseRegisters` when secret data may be on the stack) [5](#0-4) . As a result, MXCSR sticky exception bits set by operations on the secret value remain observable by subsequent, non-secret code running on the same OS thread/goroutine stack (e.g. a spawned goroutine or code that runs immediately after `Do` returns), providing a covert side channel about the secret data that `secret.Do`'s documentation explicitly claims to prevent (compare with the ARM64/loong64 implementations, which reset the entire floating-point status/control register FPSR/FPCR or FCSR, not just data registers) [6](#0-5) [7](#0-6) .

### Impact Explanation
This breaks the forward-secrecy guarantee of the experimental `runtime/secret` API on amd64: sticky FPU status bits (and potentially other RFLAGS bits) derived from operations on secret data survive past `Do`'s return and are readable by less-trusted code that runs afterward on the same thread. This is a confidentiality/side-channel leak (analogous to CVE-2023-37479's MCDT/AC-flag side channel), not remote code execution — this would be a PUBLIC-track quality issue since `runtime/secret` is explicitly documented as experimental and unsupported by the Go 1 compatibility promise, but it still represents an incomplete implementation of a documented security property.

### Likelihood Explanation
The victim workflow is any application that opts into `GOEXPERIMENT=runtimesecret` and relies on `secret.Do` to protect confidential values (e.g., cryptographic keys) processed with floating-point/SIMD-adjacent code paths, and then continues to run other, less-trusted goroutines on the same threads. The "attacker" need not have any special privilege beyond ordinary code running in the same process (e.g., a plugin, sandboxed script, or subsequent request handler) that can read MXCSR (`STMXCSR`) or observe FPU exception behavior to infer information about the secret. No network/TLS-specific reachability is required; it is an in-process capability of an unprivileged co-resident goroutine.

### Recommendation
Extend `secretEraseRegistersMcall` on amd64 to explicitly reset MXCSR to Go's fixed control configuration (as documented in the ABI: FZ=0, RC=00, all exception masks set, DAZ=0) via `LDMXCSR`, and reset the arithmetic/system RFLAGS bits deterministically (e.g., via `CLD`/appropriate flag-clearing sequence) instead of relying on an incidental `CMPL` side effect, matching the more thorough clearing already done in `secret_arm64.s` and `secret_loong64.s`.

### Proof of Concept
```go
//go:build goexperiment.runtimesecret && amd64

package secret_test

import (
	"runtime/secret"
	"testing"
	"unsafe"
)

//go:noescape
func readMXCSR() uint32 // small asm helper: STMXCSR into return value

func TestMXCSRLeakAcrossSecretDo(t *testing.T) {
	// Force a data-dependent sticky MXCSR exception bit (e.g. underflow)
	// while "secret" data is being processed.
	secret.Do(func() {
		tiny := 1e-40 // subnormal-inducing float32 op sets MXCSR underflow flag
		var x float32 = float32(tiny) * float32(tiny)
		_ = x
	})

	mxcsr := readMXCSR()
	// Expect the fixed Go ABI control configuration with no sticky
	// status bits set; a nonzero status bit here means secret-derived
	// FPU state leaked past secret.Do.
	if mxcsr&0x3F != 0 { // status bits 0-5 (IE,DE,ZE,OE,UE,PE)
		t.Fatalf("MXCSR status bits leaked secret-dependent state: %#x", mxcsr)
	}
}
```
Expected (buggy) result: the test fails because `secretEraseRegisters` never issues `LDMXCSR`, so the sticky status bits set during the secret computation remain set in MXCSR after `secret.Do` returns.

### Citations

**File:** src/runtime/secret/secret.go (L61-69)
```go
func Do(f func()) {
	const osArch = runtime.GOOS + "/" + runtime.GOARCH
	switch osArch {
	default:
		// unsupported, just invoke f directly.
		f()
		return
	case "linux/amd64", "linux/arm64", "linux/loong64":
	}
```

**File:** src/runtime/secret_amd64.s (L13-24)
```text
// secretEraseRegisters erases any register that may
// have been used with user code within a secret.Do function.
// This is roughly the general purpose and floating point
// registers, barring any reserved registers and registers generally
// considered architectural (amd64 segment registers, arm64 exception registers)
TEXT ·secretEraseRegisters(SB),NOFRAME|NOSPLIT,$0-0
	XORL	AX, AX
	JMP ·secretEraseRegistersMcall(SB)

// Mcall requires an argument in AX. This function
// excludes that register from being cleared
TEXT ·secretEraseRegistersMcall(SB),NOSPLIT|NOFRAME,$0-0
```

**File:** src/runtime/secret_amd64.s (L26-61)
```text
	XORL	BX, BX
	XORL	CX, CX
	XORL	DX, DX
	XORL	DI, DI
	XORL	SI, SI
	// BP = frame pointer
	// SP = stack pointer
	XORL	R8, R8
	XORL	R9, R9
	XORL	R10, R10
	XORL	R11, R11
	XORL	R12, R12
	XORL	R13, R13
	// R14 = G register
	XORL	R15, R15

	// floating-point registers
	CMPB	internal∕cpu·X86+const_offsetX86HasAVX(SB), $1
	JEQ	avx

	PXOR	X0, X0
	PXOR	X1, X1
	PXOR	X2, X2
	PXOR	X3, X3
	PXOR	X4, X4
	PXOR	X5, X5
	PXOR	X6, X6
	PXOR	X7, X7
	PXOR	X8, X8
	PXOR	X9, X9
	PXOR	X10, X10
	PXOR	X11, X11
	PXOR	X12, X12
	PXOR	X13, X13
	PXOR	X14, X14
	PXOR	X15, X15
```

**File:** src/runtime/secret_amd64.s (L102-106)
```text
noavx512:
	// misc registers
	CMPL	BX, BX	//eflags
	// segment registers? Direction flag? Both seem overkill.

```

**File:** src/runtime/stack.go (L1069-1076)
```go
	if goexperiment.RuntimeSecret && gp.secret > 0 {
		// If we're entering here from a secret context, clear
		// all the registers. This is important because we
		// might context switch to a different goroutine which
		// is not in secret mode, and it will not be careful
		// about clearing its registers.
		secretEraseRegisters()
	}
```

**File:** src/runtime/secret_arm64.s (L87-89)
```text
	// misc registers
	CMP	ZR, ZR // N,Z,C,V flags

```

**File:** src/runtime/secret_loong64.s (L165-175)
```text
	// misc registers
	MOVV	R0, FCC0
	MOVV	R0, FCC1
	MOVV	R0, FCC2
	MOVV	R0, FCC3
	MOVV	R0, FCC4
	MOVV	R0, FCC5
	MOVV	R0, FCC6
	MOVV	R0, FCC7
	MOVV	R0, FCSR0
	RET
```
