### Title
Unbounded array-length trusted from calldata in `BatchValidateSign` precompile causes uncontrolled memory allocation - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
The nginx advisory describes an over-read/excessive-allocation bug where a length value taken from an untrusted upstream response is used to size a buffer without validation. The same bug class exists in java-tron's `BatchValidateSign` TVM precompiled contract: an array-length word is read directly out of attacker-supplied `CALL` data and used to allocate Java arrays with no upper bound unless a specific hard-fork flag is active.

### Finding Description
`BatchValidateSign.doExecute()` parses the calldata into `DataWord[] words` and only bounds-checks the declared signature/address array sizes when `VMConfig.allowTvmSelfdestructRestriction()` is enabled: [1](#0-0) 

When that flag is not active, execution falls straight to `extractBytesArray(...)` and `extractBytes32Array(...)`, both of which take the array length directly from a caller-controlled `DataWord`: [2](#0-1) 

`len` is `words[offset].intValueSafe()` — an attacker-chosen 32-byte word from calldata that is converted straight into a Java array size (`new byte[len][]`) with no `MAX_SIZE`/upper-bound check outside the flag-gated branch. Energy metering for this precompile is derived only from the raw `data.length` of the ABI-encoded call: [3](#0-2) 

This means the energy charged is decoupled from the actual value used as the array-size word: an attacker can supply a small `data.length` (cheap energy) while pointing `words[1]`/`words[2]` offsets at a word whose value is a large integer (up to `Integer.MAX_VALUE`), producing a huge, essentially free-to-request allocation attempt (`new byte[len][]`), mirroring the nginx SCGI/uwsgi pattern of trusting an untrusted length field for buffer sizing.

### Impact Explanation
An unauthenticated party who can broadcast a transaction/contract call (any transaction sender or deployed-contract caller) can invoke the `BatchValidateSign` precompile with a crafted, cheap-energy payload that forces the node to attempt a very large heap allocation. Repeated/concurrent calls from many transactions in a block can generate memory pressure/GC storms across the node process, degrading or crashing the validating/full node — consistent with the "resource exhaustion leading to node crash/halt" impact class. This directly parallels the referenced nginx CVE's impact (excessive memory allocation from an untrusted length triggering worker crash/restart), except here the "MITM upstream" role is played by the transaction sender who fully controls the calldata word used as an array length.

### Likelihood Explanation
Likelihood is Medium: the vulnerable path is only reachable when `VMConfig.allowTvmSelfdestructRestriction()` is disabled — i.e., on chains/private nets that have not yet activated that particular hard-fork proposal, or in any test/consensus configuration lacking the flag. On the main public chain where the flag has already been activated, the specific `sigArraySize`/`addrArraySize` bound check mitigates this exact trigger, but the underlying pattern (`extractBytesArray`/`extractBytes32Array` trusting an unvalidated length word) remains in the code and is exercised by the legacy branch, so any fork/branch/private deployment still running pre-flag semantics is exposed with a single crafted, low-cost transaction.

### Recommendation
Move the `MAX_SIZE` bound check on `sigArraySize`/`addrArraySize` (and any other length word consumed by `extractBytesArray`, `extractSigArray`, `extractBytes32Array`) out of the `allowTvmSelfdestructRestriction()` conditional so it is enforced unconditionally, and make `getEnergyForData` account for the actual declared array sizes (not just raw `data.length`) so energy cost scales with the memory/CPU the call can force the node to allocate.

### Proof of Concept
Construct calldata for `BatchValidateSign` such that:
- `words[0]` = arbitrary hash
- `words[1]` = an offset pointing to a word whose value (used as `len` in `extractBytesArray`/`extractSigArray`) is set to a very large value (e.g. `0x7FFFFFFF`)
- `words[2]` similarly crafted for `extractBytes32Array`
- Keep the overall `data.length` minimal so `getEnergyForData` charges a small energy amount.

Submit this as a `CALL` to the `BatchValidateSign` precompiled address on a chain/network where `allowTvmSelfdestructRestriction` is not yet enabled. `extractBytesArray`/`extractBytes32Array` will attempt `new byte[0x7FFFFFFF][]`, forcing a multi-gigabyte allocation attempt for near-zero energy cost, which can be repeated across many transactions in rapid succession to exhaust node memory.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L390-412)
```java
  private static byte[][] extractBytes32Array(DataWord[] words, int offset) {
    int len = words[offset].intValueSafe();
    byte[][] bytes32Array = new byte[len][];
    for (int i = 0; i < len; i++) {
      bytes32Array[i] = words[offset + i + 1].getData();
    }
    return bytes32Array;
  }

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1165-1177)
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
```
