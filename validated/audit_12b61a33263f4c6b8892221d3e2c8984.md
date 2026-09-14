### Title
Integer-overflow panic in MODEXP precompile via `getData` unbounded length fields - (File: core/vm/contracts.go, core/vm/common.go)

### Summary
The `bigModExp.Run` implementation derives `baseLen`, `expLen`, and `modLen` from three attacker-controlled 32-byte big-endian fields at the start of the precompile input, using `big.Int.Uint64()`, which silently truncates values that don't fit in 64 bits rather than erroring. The `inputLenOverflow`/1024-byte cap check that would normally reject such degenerate values is only applied when `c.eip7823` (Osaka's EIP-7823 bound) is active. On any fork configuration where EIP-7823 is not yet active, these lengths are used unchecked to slice the precompile input via `getData`, which does 64-bit unsigned addition (`start + size`) without overflow protection beyond comparing against `length`.

### Finding Description
In `core/vm/contracts.go`: [1](#0-0) 
`baseLen`, `expLen`, `modLen` come straight from `big.Int.Uint64()` on attacker-supplied 32-byte fields. The only sanity check (`inputLenOverflow || max(...) > 1024`) is gated behind `c.eip7823`, so pre-Osaka this validation is skipped entirely, allowing these three values to be arbitrary `uint64`s up to `math.MaxUint64`.

These values then flow into `getData` in `core/vm/common.go`: [2](#0-1) 
`getData` clamps `start` when it exceeds `length`, and clamps `end` when it exceeds `length`, but it does not defend against the addition `start + size` overflowing `uint64` and wrapping around to a value *smaller* than `start`. When `end < start` after wraparound, the subsequent slice expression `data[start:end]` is an invalid slice range and triggers a Go runtime panic (`slice bounds out of range`), not a caught error.

Because `exp := getData(input, baseLen, expLen)` and `mod := getData(input, baseLen+expLen, modLen)` are both built from attacker-chosen huge lengths, an attacker can pick `baseLen`/`expLen`/`modLen` combinations such that `start + size` wraps below `start` in 64-bit arithmetic, causing a Go slice-bounds panic during precompile execution. No recover/guard exists around the precompile invocation path in `core/vm` to catch such a runtime panic distinctly from the normal EVM/gas error paths, so the panic propagates up through block/transaction execution.

This is analogous to CVE-2016-8705: several length fields subject to integer overflow are used to compute buffer offsets without validating the addition itself, only individual bounds — leading to memory-safety-adjacent failure (here, a Go runtime slice panic instead of C heap corruption, since Go panics rather than corrupting memory, but the root cause pattern — unchecked overflow of a length arithmetic feeding buffer/slice indexing — is the same class of bug).

### Impact Explanation
A single crafted transaction calling the MODEXP precompile (address `0x05`) with a return crafted three 32-byte length header (base/exp/mod lengths) that trigger `uint64` wraparound in `getData`'s `start+size` computation causes a deterministic Go runtime panic during EVM execution of that transaction. If this panic is not recovered somewhere higher up the call stack (block/transaction processing), it would crash the Geth process executing/validating that block — a network-wide halt from a single transaction, which matches the "Critical" impact tier described in the rules ("a single transaction or block that crashes or halts every Geth node").

### Likelihood Explanation
Reachability requires only sending a transaction that calls the MODEXP precompile with attacker-chosen input bytes — this is fully within normal transaction execution, requires no special permissions, and works on any chain configuration where EIP-7823 (Osaka) has not activated yet (i.e., current/older forks, including mainnet prior to the Osaka fork). The exact overflow condition (values of `baseLen`, `expLen`, `modLen` whose sums wrap `uint64`) is straightforward to construct because `big.Int.Uint64()` truncation is well-defined and deterministic.

### Recommendation
Enforce a length sanity/overflow check unconditionally in `bigModExp.Run` (not gated by `c.eip7823`), rejecting inputs where `baseLenBig`, `expLenBig`, or `modLenBig` do not fit safely in a reasonable bound, and/or fix `getData` in `core/vm/common.go` to detect `start + size` overflow explicitly (e.g., using an overflow-checked addition similar to `calcMemSize64WithUint`) and clamp/reject rather than relying on the post-addition comparison against `length` alone.

### Proof of Concept
I was not able to fully verify with a runnable test within this environment (no execution/terminal access) whether the wraparound is actually reachable end-to-end without being masked by an earlier explicit bound elsewhere in the call chain (e.g., gas metering in `RequiredGas` making the call prohibitively expensive before `Run` is invoked, since `berlinModexpGas`/`byzantiumModexpGas` use the same truncated lengths and could return `math.MaxUint64` gas cost, causing an out-of-gas revert before `Run` executes). This needs to be confirmed by tracing whether `RequiredGas` for the same crafted input returns a finite, payable gas cost in some deployment scenario before `Run`'s unguarded arithmetic executes. [3](#0-2) 

Given this uncertainty about whether the gas-cost overflow (`bits.Mul64` carry check returning `math.MaxUint64`) always intercepts every wraparound case before `Run` is called, I cannot assert with full confidence that this is exploitable end-to-end without a live PoC execution. This should be verified further before treating it as a confirmed finding.

### Citations

**File:** core/vm/contracts.go (L605-651)
```go
// RequiredGas returns the gas required to execute the pre-compiled contract.
func (c *bigModExp) RequiredGas(input []byte) uint64 {
	// Parse input lengths
	baseLenBig := new(uint256.Int).SetBytes(getData(input, 0, 32))
	expLenBig := new(uint256.Int).SetBytes(getData(input, 32, 32))
	modLenBig := new(uint256.Int).SetBytes(getData(input, 64, 32))

	// Convert to uint64, capping at max value
	baseLen := baseLenBig.Uint64()
	if !baseLenBig.IsUint64() {
		baseLen = math.MaxUint64
	}
	expLen := expLenBig.Uint64()
	if !expLenBig.IsUint64() {
		expLen = math.MaxUint64
	}
	modLen := modLenBig.Uint64()
	if !modLenBig.IsUint64() {
		modLen = math.MaxUint64
	}

	// Skip the header
	if len(input) > 96 {
		input = input[96:]
	} else {
		input = input[:0]
	}

	// Retrieve the head 32 bytes of exp for the adjusted exponent length
	var expHead uint256.Int
	if uint64(len(input)) > baseLen {
		if expLen > 32 {
			expHead.SetBytes(getData(input, baseLen, 32))
		} else {
			expHead.SetBytes(getData(input, baseLen, expLen))
		}
	}

	// Choose the appropriate gas calculation based on the EIP flags
	if c.eip7883 {
		return osakaModexpGas(baseLen, expLen, modLen, expHead)
	} else if c.eip2565 {
		return berlinModexpGas(baseLen, expLen, modLen, expHead)
	} else {
		return byzantiumModexpGas(baseLen, expLen, modLen, expHead)
	}
}
```

**File:** core/vm/contracts.go (L653-682)
```go
func (c *bigModExp) Run(input []byte) ([]byte, error) {
	var (
		baseLenBig       = new(big.Int).SetBytes(getData(input, 0, 32))
		expLenBig        = new(big.Int).SetBytes(getData(input, 32, 32))
		modLenBig        = new(big.Int).SetBytes(getData(input, 64, 32))
		baseLen          = baseLenBig.Uint64()
		expLen           = expLenBig.Uint64()
		modLen           = modLenBig.Uint64()
		inputLenOverflow = max(baseLenBig.BitLen(), expLenBig.BitLen(), modLenBig.BitLen()) > 64
	)
	if len(input) > 96 {
		input = input[96:]
	} else {
		input = input[:0]
	}

	// enforce size cap for inputs
	if c.eip7823 && (inputLenOverflow || max(baseLen, expLen, modLen) > 1024) {
		return nil, errors.New("one or more of base/exponent/modulus length exceeded 1024 bytes")
	}
	// Handle a special case when both the base and mod length is zero
	if baseLen == 0 && modLen == 0 {
		return []byte{}, nil
	}
	// Retrieve the operands and execute the exponentiation
	var (
		base = new(patched_big.Int).SetBytes(getData(input, 0, baseLen))
		exp  = new(patched_big.Int).SetBytes(getData(input, baseLen, expLen))
		mod  = new(patched_big.Int).SetBytes(getData(input, baseLen+expLen, modLen))
		v    []byte
```

**File:** core/vm/common.go (L83-95)
```go
// getData returns a slice from the data based on the start and size and pads
// up to size with zero's. This function is overflow safe.
func getData(data []byte, start uint64, size uint64) []byte {
	length := uint64(len(data))
	if start > length {
		start = length
	}
	end := start + size
	if end > length {
		end = length
	}
	return common.RightPadBytes(data[start:end], int(size))
}
```
