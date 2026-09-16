### Title
Unbounded attacker-controlled array-length fields in `PrecompiledContracts` batch-signature helpers cause OOM/crash (CVE-2016-2538 analog) - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
The CVE describes QEMU's USB-Net NDIS handler trusting attacker-supplied length fields inside a message to allocate/copy buffers without validating them against the real payload size, causing OOM/crash or OOB reads. `PrecompiledContracts.java` contains three helper methods — `extractBytes32Array`, `extractBytesArray`, and `extractSigArray` — that follow the identical anti-pattern: they read a 32-byte "array length" word directly out of attacker-controlled calldata and use it, unchecked against the calldata's actual size or any sane upper bound, to size a Java array allocation and to compute subsequent byte offsets.

### Finding Description
All three helpers pull `len` straight from the caller-supplied `data`/`words` with no bound check other than `intValueSafe()`: [1](#0-0) [2](#0-1) [3](#0-2) 

In each case:
- `int len = words[offset].intValueSafe();` reads a caller-supplied word as the element count.
- `byte[][] bytesArray = new byte[len][];` (or `bytes32Array`) immediately allocates a Java array of that many elements with no comparison against `words.length`, `data.length`, or a fixed sane cap (unlike, e.g., `ModExp.execute`, which explicitly caps `baseLen/expLen/modLen` at `UPPER_BOUND` — `PrecompiledContracts.java:697-700`).
- The subsequent loop then indexes `words[offset + i + 1]` and computes byte offsets into `data` using attacker-influenced `bytesOffset`/`bytesLen` values (`extractBytesArray`, lines 405-410), which — analogous to `rndis_query_response`/`usb_net_handle_dataout` mishandling length fields — can read far outside the intended region if the values don't correspond to real data bounds, or overflow `int` arithmetic when computing `(bytesOffset + offset + 2) * WORD_SIZE`.

These helpers back precompiled contracts invoked from TVM `CALL`/`STATICCALL` to fixed precompile addresses, which is directly reachable by any unprivileged account via `TriggerSmartContract`/`TriggerConstantContract` calling a deployed contract (or the precompile address itself) with crafted calldata containing a huge or negative "length" word.

### Impact Explanation
A crafted length word (e.g., near `Integer.MAX_VALUE`) causes a huge `new byte[len][]` allocation attempt, which can throw `OutOfMemoryError` inside VM execution and — because these code paths run inside contract-call handling within `Program`/`Runtime` — can propagate as an unhandled error that crashes or destabilizes the node process for every node that must independently re-execute the same broadcast transaction to reach consensus, i.e., a chain-wide denial of service. A negative length word triggers `NegativeArraySizeException`, an unchecked runtime exception, again risking crash/abort of block/transaction processing rather than a graceful contract revert. This maps to the CVE's "cause a denial of service" impact.

### Likelihood Explanation
High: the length words are taken directly from arbitrary, attacker-supplied contract calldata with no upstream validation, and reaching these code paths only requires broadcasting a normal signed `TriggerSmartContract` transaction that calls the affected precompiled contract (or a helper contract that forwards to it) with a crafted length field — no special privilege, staking, or SR/witness status is required.

### Recommendation
Add explicit upper-bound and consistency checks before allocating array based on `len` in `extractBytes32Array`, `extractBytesArray`, and `extractSigArray`: reject/clamp `len` if it is negative or exceeds a small sane maximum (or exceeds what `words.length`/`data.length` can actually support, similar to the `UPPER_BOUND` guard already used in `ModExp`), returning a failure `Pair.of(false, EMPTY_BYTE_ARRAY)` instead of allocating unchecked memory.

### Proof of Concept
Not fully confirmed due to remaining uncertainty about exactly which public precompiled-contract entry points (e.g., which `BatchValidateSign`/signature-verification variant) call `extractBytesArray`/`extractSigArray` with a fully attacker-controlled `offset`, and about the exact semantics of `DataWord.intValueSafe()` (whether it clamps to `Integer.MAX_VALUE` or throws) — I was not able to trace the calling constructors/execute() bodies for these three helpers within the available iterations. A concrete PoC would encode a `TriggerSmartContract` call to the relevant precompile address where the calldata word at the expected "array length" offset is set to `0x7fffffff` (or `0xffffffff...` for a negative value) and observe whether the resulting allocation throws `OutOfMemoryError`/`NegativeArraySizeException` during transaction execution.

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L414-426)
```java
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
