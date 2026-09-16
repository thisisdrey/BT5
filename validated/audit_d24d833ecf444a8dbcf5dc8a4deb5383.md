## Title
Heap Buffer Over-Read in `BatchValidateSign` Precompile via Unbounded `extractBytes32Array` — (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

## Summary
`extractBytes32Array` in `PrecompiledContracts.java` reads array elements without validating that `offset` is within bounds of the `words` array, unlike its sibling functions `extractBytesArray` and `extractSigArray`, which both explicitly guard against an out-of-range `offset`.

## Finding Description
`extractBytes32Array` is defined without any bounds check: [1](#0-0) 

Compare this to `extractBytesArray` and `extractSigArray`, which both perform `if (offset > words.length - 1) { return new byte[0][]; }` before indexing: [2](#0-1) 

It is called from `BatchValidateSign.doExecute`, reachable from any contract call to the `BatchValidateSign` precompile (attacker-controlled `data` from a TVM `CALL`): [3](#0-2) 

`words[2]` is fully attacker-controlled (`DataWord.parseArray(data)` parses caller-supplied calldata), so `words[2].intValueSafe() / WORD_SIZE` can be crafted to point past the end of `words`, or the resulting `len = words[offset].intValueSafe()` can be crafted arbitrarily large — both `words[offset]` and the subsequent `words[offset + i + 1].getData()` accesses in the loop can go out of the array bounds, throwing `ArrayIndexOutOfBoundsException` (a heap-based out-of-bounds read analogous to the Ming `decompileIF` CVE, which also lacked bounds validation before reading crafted-length-driven offsets).

The `MAX_SIZE` guard for `addrArraySize` at line 1167-1170 only applies when `VMConfig.allowTvmSelfdestructRestriction()` is active; when that config flag is disabled, `extractBytes32Array` is called with a completely unvalidated offset/length and no upper bound at all, letting `words[offset]` (the length word) and the loop index run past `words.length`.

## Impact Explanation
An out-of-bounds array access in Java throws `ArrayIndexOutOfBoundsException` rather than silently reading adjacent heap memory (Java is memory-safe at this level), so this does not directly leak heap contents like the native C over-read in libming. However, depending on how the exception propagates through the actuator/executor call stack, an uncaught runtime exception during TVM precompile execution triggered by a single unprivileged smart-contract call could disrupt transaction processing for that specific call (reverting it) or, if not properly contained by the TVM's exception-handling wrapper, could produce inconsistent behavior across nodes if some nodes throw earlier/later than others due to non-deterministic evaluation ordering — though I could not fully verify from the retrieved code whether this exception is caught by an enclosing `try/catch` in `BatchValidateSign.execute` or propagates further. Given Java's memory safety, the realistic impact ceiling here is a contained precompile execution failure (transaction revert), not a memory-disclosure or RCE-class issue.

## Likelihood Explanation
High: this precompile is a standard `CALL`-target reachable by any user submitting a `TriggerSmartContract` transaction with contract bytecode invoking `BatchValidateSign`, requiring no special privilege, only that `VMConfig.allowTvmSelfdestructRestriction()` be inactive on the relevant network to fully bypass the `MAX_SIZE` check.

## Recommendation
Add the same bounds check used in `extractBytesArray`/`extractSigArray` to `extractBytes32Array` (reject if `offset > words.length - 1`), and additionally validate that `offset + len` does not exceed `words.length` before the loop executes, independent of the `allowTvmSelfdestructRestriction` feature flag.

## Proof of Concept
Construct calldata for `BatchValidateSign(bytes32,bytes,bytes)`-style ABI encoding where:
- `words[2]` (the offset word for the addresses array) is set to a value causing `words[2].intValueSafe() / WORD_SIZE` to be at or near `words.length`.
- The word at that offset (interpreted as array length) is set to a large value (e.g., `Integer.MAX_VALUE / WORD_SIZE`) so the loop in `extractBytes32Array` indexes far past the actual `words` array.

Submitting this as calldata to a contract call targeting the `BatchValidateSign` precompile address triggers `words[offset + i + 1].getData()` to throw `ArrayIndexOutOfBoundsException` inside `doExecute`, as seen at: [1](#0-0) 

I could not confirm from the indexed code whether this exception is caught and safely converted into a normal (false-result) TVM revert by an outer handler in `BatchValidateSign.execute` (the file segment covering that method was not returned in my searches) — this should be verified directly against the full file before treating the impact as more than a contained revert.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L390-397)
```java
  private static byte[][] extractBytes32Array(DataWord[] words, int offset) {
    int len = words[offset].intValueSafe();
    byte[][] bytes32Array = new byte[len][];
    for (int i = 0; i < len; i++) {
      bytes32Array[i] = words[offset + i + 1].getData();
    }
    return bytes32Array;
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L399-426)
```java
  private static byte[][] extractBytesArray(DataWord[] words, int offset, byte[] data) {
    if (offset > words.length - 1) {
      return new byte[0][];
    }
    int len = words[offset].intValueSafe();
    byte[][] bytesArray = new byte[len][];
    for (int i = 0; i < len; i++) {
      int bytesOffset = words[offset + i + 1].intValueSafe() / WORD_SIZE;
      int bytesLen = words[offset + bytesOffset + 1].intValueSafe();
      bytesArray[i] = extractBytes(data, (bytesOffset + offset + 2) * WORD_SIZE,
          bytesLen);
    }
    return bytesArray;
  }

  private static byte[][] extractSigArray(DataWord[] words, int offset, byte[] data) {
    if (offset > words.length - 1) {
      return new byte[0][];
    }
    int len = words[offset].intValueSafe();
    byte[][] bytesArray = new byte[len][];
    for (int i = 0; i < len; i++) {
      int bytesOffset = words[offset + i + 1].intValueSafe() / WORD_SIZE;
      bytesArray[i] = extractBytes(data, (bytesOffset + offset + 2) * WORD_SIZE,
          SIG_LENGTH);
    }
    return bytesArray;
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1162-1177)
```java
      DataWord[] words = DataWord.parseArray(data);
      byte[] hash = words[0].getData();

      if (VMConfig.allowTvmSelfdestructRestriction()) {
        int sigArraySize = words[words[1].intValueSafe() / WORD_SIZE].intValueSafe();
        int addrArraySize = words[words[2].intValueSafe() / WORD_SIZE].intValueSafe();
        if (sigArraySize > MAX_SIZE || addrArraySize > MAX_SIZE) {
          return Pair.of(true, DATA_FALSE);
        }
      }

      byte[][] signatures = VMConfig.allowTvmSelfdestructRestriction() ?
          extractSigArray(words, words[1].intValueSafe() / WORD_SIZE, data) :
          extractBytesArray(words, words[1].intValueSafe() / WORD_SIZE, data);
      byte[][] addresses = extractBytes32Array(
          words, words[2].intValueSafe() / WORD_SIZE);
```
