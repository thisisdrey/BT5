### Title
Unbounded attacker-controlled offset causes out-of-bounds array read in `ValidateMultiSign`/`BatchValidateSign` TVM precompiles - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
Similar to CVE-2025-58148, where a Viridian hypercall lets an unprivileged guest supply an index that `send_ipi()` uses to index `d->vcpu[]` without bounds checking, java-tron's `ValidateMultiSign` and `BatchValidateSign` TVM precompiled contracts parse an attacker-controlled 32-byte word from calldata, divide it by `WORD_SIZE`, and use the resulting value directly as an array index into the `words[]` array with no range check before the first out-of-bounds access occurs.

### Finding Description
Both precompiles parse the raw calldata into a fixed-size `DataWord[] words` array sized to `rawData.length / WORD_SIZE` [1](#0-0) . They then compute an offset entirely from attacker-supplied calldata (`words[3]` for `ValidateMultiSign`, `words[1]`/`words[2]` for `BatchValidateSign`) and immediately use it to index back into the same `words[]` array before any bounds validation: [2](#0-1) [3](#0-2) 

The only defensive check present, `extractBytesArray`/`extractSigArray`'s `if (offset > words.length - 1) return new byte[0][];`, executes only *after* the initial unchecked access `words[words[3].intValueSafe() / WORD_SIZE]` (line 1067) and `words[words[1].intValueSafe() / WORD_SIZE]` / `words[words[2].intValueSafe() / WORD_SIZE]` (lines 1166-1167) has already occurred [4](#0-3) . Because `intValueSafe()` can return arbitrary attacker-chosen int values (including negative numbers via overflow or values far larger than `words.length`), this first index computation and array dereference is unguarded and can throw a Java `ArrayIndexOutOfBoundsException` before the guard clause is ever reached — directly analogous to `send_ipi()` dereferencing `d->vcpu[idx]` before validating `idx` against `d->max_vcpus`.

The optional ABI-encoding validation gated by `VMConfig.allowTvmOsaka()` (`isValidAbiEncoding`) only checks that the overall calldata length matches a fixed header/item-word structure; it does not validate that the *value* encoded in the offset field (`words[3]`, `words[1]`, `words[2]`) actually points within the bounds of the parsed `words[]` array [5](#0-4) .

### Impact Explanation
Any account can trigger this by writing or calling a smart contract that invokes the `ValidateMultiSign` or `BatchValidateSign` precompile address with crafted calldata where the offset word decodes to an out-of-range index. This is directly reachable from a single `TriggerSmartContract` transaction — no special privilege, SR/witness/committee role, or node-level access is required. Depending on whether the resulting `ArrayIndexOutOfBoundsException` is caught by an enclosing handler in the TVM/precompiled-contract call path, this can at minimum cause unexpected, uncontrolled exceptions during contract execution, and at worst — if uncaught along that call path — cause a node crash / halt while validating or executing a broadcast transaction, which is a valid high-impact outcome per the CVE's bug class (uncontrolled OOB access from an index derived from untrusted input).

### Likelihood Explanation
High. The precompile is called through ordinary `CALL`/`STATICCALL` opcodes from any deployed smart contract to a fixed, publicly documented precompile address, with fully attacker-controlled calldata bytes. No preconditions (existing account state, prior transactions, or elevated permissions) are needed — an attacker only needs to deploy a trivial contract and call `TriggerSmartContract`, making this trivially reachable by "an unprivileged transaction broadcaster [or] contract deployer."

### Recommendation
Before using any calldata-derived index/offset to dereference `words[]` (or any other array) in `ValidateMultiSign` and `BatchValidateSign`, validate the computed index against `words.length` (and against negative values) and return the existing `DATA_FALSE`/`Pair.of(false, EMPTY_BYTE_ARRAY)` failure path if out of range — mirroring the existing but currently too-late guard in `extractBytesArray`/`extractSigArray`. Apply the same bound check to every one of `words[3]`, `words[1]`, and `words[2]`-derived offsets prior to their first use as an index.

### Proof of Concept
1. Deploy a minimal contract that performs a raw `call`/`staticcall` to the `ValidateMultiSign` precompile address (as exposed via `PrecompiledContracts.getContractForAddress`, gated by `VMConfig.allowTvmVote`/related feature flags) with calldata consisting of:
   - word0: arbitrary 20-byte address
   - word1: arbitrary permission id
   - word2: arbitrary hash data word
   - word3: a large value (e.g., `0xFFFFFFFF` or a value such that `value/32` exceeds the number of 32-byte words actually present in the supplied calldata)
2. Broadcast a `TriggerSmartContract` transaction invoking this call.
3. During `execute(rawData)`, the expression `words[words[3].intValueSafe() / WORD_SIZE]` (line 1067) computes an index far beyond `words.length` and throws `ArrayIndexOutOfBoundsException` before the length guard in `extractBytesArray`/`extractSigArray` can run.

Note: I was unable to confirm within the available tool budget whether this exception is caught by an outer try/catch in `Program`'s precompiled-call dispatch path (which would downgrade the impact to a reverted transaction) or propagates further; this should be verified by tracing `Program.callToPrecompiledAddress` (or equivalent) exception handling to determine the exact blast radius (transaction revert vs. node-level crash).

### Citations

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L432-438)
```java
  private static boolean isValidAbiEncoding(byte[] data, int headerWords, int itemWords) {
    if (data == null || data.length % WORD_SIZE != 0) {
      return false;
    }
    long tail = subtractExact(data.length, multiplyExact(headerWords, WORD_SIZE));
    return tail > 0 && tail % multiplyExact(itemWords, WORD_SIZE) == 0;
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1057-1057)
```java
      DataWord[] words = DataWord.parseArray(rawData);
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1066-1074)
```java
      if (VMConfig.allowTvmSelfdestructRestriction()) {
        int sigArraySize = words[words[3].intValueSafe() / WORD_SIZE].intValueSafe();
        if (sigArraySize > MAX_SIZE) {
          return Pair.of(true, DATA_FALSE);
        }
      }
      byte[][] signatures = VMConfig.allowTvmSelfdestructRestriction() ?
          extractSigArray(words, words[3].intValueSafe() / WORD_SIZE, rawData) :
          extractBytesArray(words, words[3].intValueSafe() / WORD_SIZE, rawData);
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
