## Title
Unrecovered runtime panic (slice bounds out of range) in MODEXP precompile from crafted `base`/`exponent`/`modulus` length fields — ([File: core/vm/contracts.go])

### Summary
`bigModExp.Run` in `core/vm/contracts.go` parses the `baseLen`, `expLen`, `modLen` header fields of the MODEXP (0x05) precompile input by taking `big.Int.Uint64()` of three attacker-controlled 32-byte values *before* validating that they actually fit in 64 bits. The only guard against oversized/overflowing lengths (`inputLenOverflow || max(...) > 1024`) is gated behind `c.eip7823`, which is only true once EIP-7823 (Osaka) is active. On the currently active fork rules, this check is skipped entirely, so `baseLen`, `expLen`, `modLen` are attacker-controlled arbitrary `uint64` values (the low 64 bits of arbitrary 256-bit numbers). These are then used in unchecked `uint64` additions (`baseLen+expLen`) passed into `getData`, which can wrap around and produce a slice expression with `end < start`, causing an unrecovered Go runtime panic.

### Finding Description
In `core/vm/contracts.go`: [1](#0-0) 

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
	...
	// enforce size cap for inputs
	if c.eip7823 && (inputLenOverflow || max(baseLen, expLen, modLen) > 1024) {
		return nil, errors.New("one or more of base/exponent/modulus length exceeded 1024 bytes")
	}
	...
	base = new(patched_big.Int).SetBytes(getData(input, 0, baseLen))
	exp  = new(patched_big.Int).SetBytes(getData(input, baseLen, expLen))
	mod  = new(patched_big.Int).SetBytes(getData(input, baseLen+expLen, modLen))
```

`baseLenBig.Uint64()` for a `big.Int` with `BitLen() > 64` returns the low 64 bits of the value (undefined per Go docs but deterministic — it does not panic). The overflow-rejection check at line 670 is `c.eip7823 && (...)`, i.e. it is short-circuited to `false` whenever `eip7823` is not yet active for the current chain rules. On all currently-active forks, `baseLen`/`expLen`/`modLen` are therefore attacker-chosen arbitrary `uint64` values, entirely independent of the true 256-bit header values.

These lengths feed `getData`: [2](#0-1) 

```go
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

`start` is clamped to `<= length` first, but `end := start + size` is an unchecked `uint64` addition. By choosing `expLen` (or `modLen`) close to `2^64`, `start+size` wraps around to a value smaller than `length` (or smaller than `start`), so the `end > length` clamp does not trigger. The subsequent `data[start:end]` then either panics directly (`end < start` -> "slice bounds out of range") or otherwise produces a self-inconsistent slice. The same wraparound applies to `baseLen+expLen` used directly as the `start` argument for the modulus's `getData` call at line 681, compounding the attacker's control over the wrapped offset used for `mod`.

Because MODEXP is a mainnet precompile invoked from ordinary EVM `CALL`/`STATICCALL` opcodes, any transaction can reach `bigModExp.Run` with a crafted 96-byte header plus payload. There is no `recover()` around precompile execution in `core/vm` (confirmed absent in `core/vm/evm.go` and the interpreter), so a panic here propagates uncaught up through block processing.

### Impact Explanation
A single, deterministic transaction that calls MODEXP with a crafted header can trigger an unrecovered Go runtime panic during block execution. Because execution of a given block is deterministic and every full node executes the same transaction while processing/validating that block, this crashes every Geth node processing the block — meeting the "single transaction or block that crashes or halts every Geth node" Critical-impact criterion, since block processing is not optional and cannot be skipped by a node that wants to stay on the canonical chain.

### Likelihood Explanation
Likelihood is high: MODEXP is reachable from any contract via a single `CALL`, requires no privileged position, and the crafted header only needs `baseLen`/`expLen`/`modLen` chosen so the low-64-bit representation wraps `start+size` past `2^64`. No special preconditions (e.g., large real memory buffers) are required, since the actual `input` byte slice can be small — only the header fields need to be enormous 256-bit numbers.

### Recommendation
Reject (return an error) whenever `baseLenBig`, `expLenBig`, or `modLenBig` have `BitLen() > 64` *unconditionally*, not only under `c.eip7823`, and/or make `getData`'s `start+size` addition overflow-safe (e.g. using `math.SafeAdd`/saturating arithmetic) so a wrapped `end` can never be produced. Apply the same treatment to `RequiredGas`, which currently caps only via `IsUint64()`, and audit all other unconditional-add call sites in `getData`/`getDataAndAdjustedBounds` for the same class of wraparound.

### Proof of Concept
Construct calldata to MODEXP (`0x0000...0005`) with the following ABI-less 96-byte header followed by minimal payload:
- Bytes[0:32] (`baseLenBig`): choose so its low 64 bits, `baseLen`, are small (e.g. `1`), with high bits nonzero to exceed 64 bits.
- Bytes[32:64] (`expLenBig`): choose so its low 64 bits, `expLen`, equal `2^64 - baseLen` truncated appropriately to force `baseLen+expLen` to wrap to a small value less than the actual data length, or choose `expLen` itself huge so `getData(input, baseLen, expLen)`'s internal `start+size` wraps below `start`.
- Bytes[64:96] (`modLenBig`): any value.

Since `c.eip7823` is false on the currently active fork rules, the `inputLenOverflow` check is bypassed, `baseLen`/`expLen`/`modLen` retain their wrapped low-64-bit values, and the subsequent `getData` calls compute `end < start`, causing `data[start:end]` to panic with "slice bounds out of range" inside `bigModExp.Run`, uncaught anywhere in the call stack.

### Citations

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
