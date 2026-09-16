### Title
Unbounded attacker-controlled array length in `extractBytes32Array` causes `ArrayIndexOutOfBoundsException` reachable via `BatchValidateSign` precompile - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
The Amlogic c3 ISP driver bug fixed by the referenced CVE is a classic "attacker-controlled count used to index/write into a fixed-size array without validating the count against the array's real bound" pattern (`zones_num` vs `zone_weight`). The same bug class exists in java-tron's `BatchValidateSign` / `ValidateMultiSign` precompiled contracts, where a length value decoded straight from calldata (`words[offset].intValueSafe()`) is used to loop and index into the `words` array via `extractBytes32Array`, with no bound check on the length or the resulting index range.

### Finding Description
`extractBytesArray` and `extractSigArray` at least perform a partial guard (`if (offset > words.length - 1) return new byte[0][];`) before reading the length word, but `extractBytes32Array` has no such guard at all: [1](#0-0) 

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

`len` is fully attacker-controlled (an ABI word decoded via `DataWord.intValueSafe()`, which can be up to `Integer.MAX_VALUE`), and it is never checked against `words.length`. `words` itself is derived by `DataWord.parseArray(data)` from the raw calldata passed into the precompile, so its length is small and directly proportional to the transaction's calldata size. This is called from `BatchValidateSign.doExecute`: [2](#0-1) 

```java
byte[][] signatures = VMConfig.allowTvmSelfdestructRestriction() ?
    extractSigArray(words, words[1].intValueSafe() / WORD_SIZE, data) :
    extractBytesArray(words, words[1].intValueSafe() / WORD_SIZE, data);
byte[][] addresses = extractBytes32Array(
    words, words[2].intValueSafe() / WORD_SIZE);
int cnt = signatures.length;
if (cnt == 0 || cnt > MAX_SIZE || signatures.length != addresses.length) {
  return Pair.of(true, DATA_FALSE);
}
```

The `MAX_SIZE`/`sigArraySize`/`addrArraySize` bound check in the code above is only performed when `VMConfig.allowTvmSelfdestructRestriction()` is enabled: [3](#0-2) 

```java
if (VMConfig.allowTvmSelfdestructRestriction()) {
  int sigArraySize = words[words[1].intValueSafe() / WORD_SIZE].intValueSafe();
  int addrArraySize = words[words[2].intValueSafe() / WORD_SIZE].intValueSafe();
  if (sigArraySize > MAX_SIZE || addrArraySize > MAX_SIZE) {
    return Pair.of(true, DATA_FALSE);
  }
}
```

If this hardfork flag is not active (or on any historical block/replay before the fork), `extractBytes32Array` is invoked directly with an attacker-supplied `len`, and it will attempt `new byte[len][]` (potential huge allocation / OOM for large `len`) followed by `words[offset + i + 1]`, which throws `ArrayIndexOutOfBoundsException` almost immediately once `i` exceeds the small actual `words.length`. This exception is not caught inside `extractBytes32Array` or at its call site inside `doExecute`; it is only caught by the generic `catch (Throwable t)` wrapper in `BatchValidateSign.execute`: [4](#0-3) 

so a crash there does not propagate further up in that code path — but the direct analog of the original CVE (indexing without bound validation) is present and identical in structure to the smatch-flagged pattern (`zones_num`/`zone_weight`).

Note: I was unable to fully trace, within the available index, how `PrecompiledContract.execute()` exceptions are surfaced by the caller in `Program.java`/`OperationActions.callToPrecompiledAddress` (the relevant call sites did not return full method bodies in the search results), so I cannot conclusively state whether an uncaught exception from this code path (if reached without the `catch(Throwable)` wrapper, e.g. via `ValidateMultiSign.execute`, which does *not* wrap the `extractSigArray`/`extractBytesArray` calls in a try/catch) would crash block processing or merely revert the single transaction.

### Impact Explanation
Best case (if the exception is caught and converted into a failed/reverted call): this is a denial-of-service against the specific transaction/contract call, not a high-severity issue. Worst case (if the exception propagates out of `PrecompiledContracts.execute`/`ValidateMultiSign.execute`, which has no enclosing try/catch around the vulnerable calls at lines 1072–1074): an uncaught `RuntimeException`/`OutOfMemoryError` during TVM execution triggered by a single, unprivileged smart-contract call could disrupt block application for all full nodes replaying the same transaction, since TVM execution is deterministic and mandatory for consensus. I could not confirm this worst case with certainty from the available code, so severity is capped at Medium/High depending on that unconfirmed control-flow detail rather than definitively Critical.

### Likelihood Explanation
This is trivially reachable: any account can call the `BatchValidateSign` (and `ValidateMultiSign`) precompiled contract with crafted calldata containing an arbitrarily large "array length" header word for the address array pointer, requiring no special privileges, stake, or prior state setup. The only mitigating factor is that on chains/forks where `VMConfig.allowTvmSelfdestructRestriction()` is already active, the `MAX_SIZE` check occurs before the vulnerable extraction, closing the gap for `BatchValidateSign`'s `addresses` path — but the underlying `extractBytes32Array` helper itself remains unguarded and could be reached by any future or historical code path that doesn't enforce that config flag.

### Recommendation
Add an explicit bound check inside `extractBytes32Array` (mirroring the `offset > words.length - 1` guard already used in `extractBytesArray`/`extractSigArray`), and additionally validate that `len >= 0` and `offset + len + 1 <= words.length` before allocating or looping, returning an empty array / failure result otherwise. Apply the same length validation unconditionally in `ValidateMultiSign`/`BatchValidateSign`, not only when `allowTvmSelfdestructRestriction()` is enabled, so it is not tied to a hardfork toggle.

### Proof of Concept
Construct calldata for `BatchValidateSign(bytes32,bytes[],bytes32[])` where:
- word[0] = hash (32 bytes, arbitrary)
- word[1] = offset pointing to the signatures array header (valid small array, e.g. length 1)
- word[2] = offset pointing to the addresses array header, whose length word is set to a large value (e.g. `0x7fffffff`)

Deploy a trivial contract that performs a `staticcall`/`call` to the `BatchValidateSign` precompile address with this calldata on a node where `allowTvmSelfdestructRestriction()` is not yet enabled. `extractBytes32Array` will attempt `new byte[0x7fffffff][]` (likely throwing `OutOfMemoryError`) or, for a moderately large length just beyond `words.length`, will throw `ArrayIndexOutOfBoundsException` on `words[offset + i + 1]` while iterating — demonstrating the unvalidated-length buffer/array overflow analogous to the reported kernel CVE's `zones_num`/`zone_weight` mismatch.

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1165-1171)
```java
      if (VMConfig.allowTvmSelfdestructRestriction()) {
        int sigArraySize = words[words[1].intValueSafe() / WORD_SIZE].intValueSafe();
        int addrArraySize = words[words[2].intValueSafe() / WORD_SIZE].intValueSafe();
        if (sigArraySize > MAX_SIZE || addrArraySize > MAX_SIZE) {
          return Pair.of(true, DATA_FALSE);
        }
      }
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1173-1181)
```java
      byte[][] signatures = VMConfig.allowTvmSelfdestructRestriction() ?
          extractSigArray(words, words[1].intValueSafe() / WORD_SIZE, data) :
          extractBytesArray(words, words[1].intValueSafe() / WORD_SIZE, data);
      byte[][] addresses = extractBytes32Array(
          words, words[2].intValueSafe() / WORD_SIZE);
      int cnt = signatures.length;
      if (cnt == 0 || cnt > MAX_SIZE || signatures.length != addresses.length) {
        return Pair.of(true, DATA_FALSE);
      }
```
