I found a strong analog: an unbounded, unchecked offset/index calculation while decoding attacker-controlled ABI data in the `BatchValidateSign` precompile, structurally identical to the CVE's root cause (a die-ref/offset value taken directly from untrusted data and used to index into an array without bounds validation).

### Title
Unbounded array index from attacker-controlled ABI offset causes crash in `BatchValidateSign` precompile - ([File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java])

### Summary
`BatchValidateSign.doExecute` (and `ValidateMultiSign`) parse ABI-encoded call data into a `DataWord[] words` array and then use attacker-supplied word values as array offsets to call `extractBytes32Array(words, words[2].intValueSafe() / WORD_SIZE)`. Unlike the sibling helper `extractBytesArray`, `extractBytes32Array` performs **no bounds check** on `offset` before indexing `words[offset]`, mirroring the CVE's pattern where a `DW_FORM_ref_addr` offset value taken from untrusted input is used to reference into a buffer without validating it stays within the buffer.

### Finding Description
`extractBytes32Array` is defined as: [1](#0-0) 

Contrast this with `extractBytesArray`/`extractSigArray`, which explicitly guard against the same class of value: [2](#0-1) 

`extractBytes32Array` is missing the `if (offset > words.length - 1) return new byte[0][];` guard present in the other two extractors. It is called in `BatchValidateSign.doExecute` with an offset value directly derived from attacker-controlled call data (`words[2].intValueSafe() / WORD_SIZE`): [3](#0-2) 

Because `words[2]` is fully attacker-controlled ABI input, a caller can set it to a huge value, making `offset` exceed `words.length - 1`. The subsequent `words[offset].intValueSafe()` throws `ArrayIndexOutOfBoundsException` inside `extractBytes32Array`. If `words[2]` is small, `len = words[offset].intValueSafe()` can also be set arbitrarily large (since `intValueSafe` clamps to `Integer.MAX_VALUE` but is otherwise attacker-chosen), causing the inner loop `words[offset + i + 1]` to run out of bounds almost immediately, again throwing an unguarded `ArrayIndexOutOfBoundsException`, or attempting to allocate an oversized `byte[len][]` array (`OutOfMemoryError`).

This is the direct analog of CVE-2017-15938: an offset value read from untrusted/attacker-provided data is used to index into a data structure without validating it is within bounds, causing an invalid/out-of-bounds memory access and a crash (there, a native segfault; here, an uncaught Java exception/error).

### Impact Explanation
`BatchValidateSign` is a TVM precompiled contract reachable by any contract that calls the corresponding precompile address via `CALL`/`STATICCALL` opcodes, i.e., reachable from any unprivileged, unpermissioned smart-contract invocation triggered by a normal `TriggerSmartContract` transaction. The `Pair<Boolean, byte[]> execute` wraps the call in a `try/catch (Throwable t)`, catching `ArrayIndexOutOfBoundsException` and returning a default `DATA_FALSE` result: [4](#0-3) 

Because the outer `execute` method catches `Throwable`, the immediate `ArrayIndexOutOfBoundsException` inside `doExecute`/`extractBytes32Array` is caught and does not crash the node — this significantly limits impact versus the original CVE (which caused a full application crash/DoS in `objdump`/`gdb`). However, `ValidateMultiSign`'s call path to `extractBytesArray`/`extractSigArray` is not wrapped in the same broad catch (only a narrower catch around signature recovery), so an unhandled `ArrayIndexOutOfBoundsException` from a crafted offset there could propagate further up the execution stack; I could not fully verify from the available context whether the TVM's outer opcode dispatch loop (e.g., `Program`/`VM` step handling) uniformly catches all runtime exceptions from precompiled contracts, which would determine whether this ultimately only reverts the transaction (low impact) or could destabilize node execution threads (higher impact). This uncertainty should be resolved by a deeper review of `Program`/`VM`'s exception handling around precompile invocation.

### Likelihood Explanation
High likelihood of triggering the bug: constructing the malicious payload only requires setting a single 32-byte ABI word (`words[2]`, the "addresses" offset field) to an out-of-range value in a call to the `BatchValidateSign` precompile address, which is trivially reachable from any deployed contract or via `eth_call`/`triggerConstantContract` JSON-RPC/HTTP endpoints given TRON precompiles are typically callable via low-level `call`/`staticcall`.

### Recommendation
Add the same bounds check present in `extractBytesArray`/`extractSigArray` to `extractBytes32Array`:
```java
private static byte[][] extractBytes32Array(DataWord[] words, int offset) {
  if (offset < 0 || offset > words.length - 1) {
    return new byte[0][];
  }
  int len = words[offset].intValueSafe();
  if (len < 0 || offset + len >= words.length) {
    return new byte[0][];
  }
  byte[][] bytes32Array = new byte[len][];
  for (int i = 0; i < len; i++) {
    bytes32Array[i] = words[offset + i + 1].getData();
  }
  return bytes32Array;
}
```
Additionally, audit and harden `ValidateMultiSign`'s exception handling to ensure any bounds/allocation failure from precompile input parsing is caught and converted to a well-defined `DATA_FALSE`/revert result rather than propagating as an unhandled runtime exception.

### Proof of Concept
1. Deploy or use any contract that performs a low-level `staticcall`/`call` to the `BatchValidateSign` precompile address with ABI-encoded data where:
   - `words[0]` = arbitrary hash (32 bytes)
   - `words[1]` = a valid small offset pointing to an empty/short signature array section
   - `words[2]` = a large value (e.g., `0xFFFFFFFF`), which after `/ WORD_SIZE` yields an offset far beyond `words.length`
2. Invoke the contract via a normal `TriggerSmartContract` transaction (or via `triggerConstantContract`/`eth_call` JSON-RPC for a constant call).
3. Observe `extractBytes32Array(words, offset)` throws `ArrayIndexOutOfBoundsException` at `words[offset].intValueSafe()`, confirming the unchecked offset-based array access analogous to the CVE's unchecked `DW_FORM_ref_addr` offset dereference.

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1144-1154)
```java
    @Override
    public Pair<Boolean, byte[]> execute(byte[] data) {
      try {
        return doExecute(data);
      } catch (Throwable t) {
        if (t instanceof InterruptedException){
          Thread.currentThread().interrupt();
        }
        return Pair.of(true, new byte[WORD_SIZE]);
      }
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
