## Analog Found

### Title
Missing length-header validation before array allocation in `BatchValidateSign` precompile enables unbounded memory allocation DoS - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
CVE-2018-20819 stems from Dropbox Lepton's decompressor trusting a length field embedded in a file header without validating it against the maximum allowable/actual buffer size before allocating memory, leading to a crash. The same bug class exists in java-tron's `BatchValidateSign` TVM precompiled contract: the address-array length field embedded in attacker-controlled calldata is used to allocate a Java array via `extractBytes32Array` without any upper-bound check unless a specific feature flag is active, and without any check against the actual data buffer size.

### Finding Description
`extractBytes32Array` reads a length value directly from attacker-supplied ABI-encoded calldata and immediately allocates an array of that size, with no upper bound and no validation against the remaining `words` buffer length: [1](#0-0) 

This differs from its sibling helpers `extractBytesArray` and `extractSigArray`, which at least bound-check `offset` against `words.length` before reading: [2](#0-1) 

In `BatchValidateSign.doExecute`, the `addrArraySize` bound is only enforced when the `allowTvmSelfdestructRestriction` feature is active: [3](#0-2) 

If that flag is not active (e.g., prior to activation on a given chain, or on a fork/testnet where the corresponding chain parameter has not been enabled), `extractBytes32Array` is invoked with a completely unchecked `len` derived from `words[offset].intValueSafe()`, which can be attacker-set up to `Integer.MAX_VALUE`. The energy cost for the call, computed in `getEnergyForData`, is based solely on the actual calldata length, not on the value embedded inside the ABI offset/length field, so an attacker can submit a small, cheap calldata blob whose embedded "array length" word is enormous: [4](#0-3) 

This is a direct analog of the CVE: a length header inside a parsed payload is not checked against the maximum permissible/actual size before being used to size a memory allocation.

### Impact Explanation
A crafted `CALL`/contract invocation to the `BatchValidateSign` precompile can trigger allocation of a byte-array-of-arrays sized by an attacker-controlled 32-byte word, causing very large heap allocation attempts (up to `Integer.MAX_VALUE` elements) for minimal energy cost. This can induce `OutOfMemoryError`/heavy GC pressure on the node processing the transaction. While `execute()` wraps `doExecute()` in a `catch (Throwable t)` so a single call does not propagate an unhandled exception, the underlying allocation attempt itself consumes disproportionate heap/CPU resources relative to the energy charged, and repeated invocation (cheaply, since energy accounting does not scale with the forged length) constitutes a resource-exhaustion / denial-of-service vector against any node executing the transaction — including validators applying blocks in `Manager`.

### Likelihood Explanation
Reachable by any unprivileged account that can invoke a smart contract calling the `BatchValidateSign` precompiled contract address with crafted calldata — no special privileges required. The only gating factor is whether `allowTvmSelfdestructRestriction` is active on the target chain; this is a standard, addressable, low-cost attack path once the corresponding chain parameter is not enabled (e.g., during transitional windows, private/test networks, or forks that have not activated the parameter).

### Recommendation
Apply the same bound check unconditionally (regardless of `allowTvmSelfdestructRestriction`) inside `extractBytes32Array`, mirroring the guards already present in `extractBytesArray`/`extractSigArray`: validate `len` against a fixed `MAX_SIZE` and validate `offset + len` against `words.length` before allocating, prior to allowing the size value to drive any array allocation.

### Proof of Concept
Construct calldata for `BatchValidateSign` such that:
1. `words[2]` (the "addresses" offset pointer) points to a word position within a minimally-sized `words` array.
2. The word at that offset position is set to `0x7FFFFFFF` (interpreted as `len` in `extractBytes32Array`).
3. Submit this as calldata via a `CALL` opcode to the `BatchValidateSign` precompile address in a contract, on a chain configuration where `allowTvmSelfdestructRestriction` has not been activated.

This drives `extractBytes32Array(words, offset)` to execute `new byte[0x7FFFFFFF][]`, an allocation attempt disproportionate to the small actual calldata length and its correspondingly low energy charge. [5](#0-4)

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L399-412)
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
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1137-1142)
```java
    @Override
    public long getEnergyForData(byte[] data) {
      long cnt = (data.length / WORD_SIZE - 5) / 6;
      // one sign 1500, half of ecrecover
      return cnt * ENGERYPERSIGN;
    }
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1165-1178)
```java
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
      int cnt = signatures.length;
```
