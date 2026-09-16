### Title
Unbounded Array Allocation from Attacker-Controlled Length in TVM Precompiled Contract Decoding Can Trigger Node-Crashing OutOfMemoryError - ([File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java])

### Summary
The Apache Mina SSHD advisory (CVE-2021-30129) describes a buffer-overflow/OOM condition where the SFTP/port-forwarding path allocates memory sized by an attacker-controlled length field without an upper bound, letting a remote peer exhaust server memory. `java-tron`'s TVM precompiled-contract argument decoders exhibit the same bug class: they allocate arrays sized directly from an attacker-controlled 32-byte word taken from EVM/TVM calldata, with no upper bound check against the actual size of the supplied calldata.

### Finding Description
In `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`, the helper `extractBytes32Array` decodes a "length" field straight from calldata and allocates an array of that size before any bound validation: [1](#0-0) 

`len` comes from `words[offset].intValueSafe()`. Per `DataWord.intValueSafe()`, if the attacker crafts a 32-byte word that occupies more than 4 bytes (i.e., any 256-bit value ≥ 2^32), the method deliberately clamps and returns `Integer.MAX_VALUE` instead of throwing: [2](#0-1) 

That clamped value (`Integer.MAX_VALUE ≈ 2.1 billion`) is then used directly as the allocation size `new byte[len][]`, with **no check that `len` is consistent with the actual length of `words`/`data`** supplied by the caller. The same unguarded pattern recurs in the sibling helpers `extractBytesArray` and `extractSigArray`, which only bound-check `offset` against `words.length`, not `len` itself, before allocating `new byte[len][]`: [3](#0-2) 

An array of `Integer.MAX_VALUE` object references requires on the order of tens of gigabytes on a 64-bit JVM (8+ bytes per reference plus array header), which will throw `OutOfMemoryError` — or, depending on heap sizing and GC pressure, degrade/crash the JVM process — well before any subsequent bounds/energy check can reject the transaction. Critically, the TVM's energy accounting for precompiles is based on the byte length of the *actual* `data` payload passed to `execute()`/`getEnergyForData()` (see the `Sha256`/`Identity` energy examples at lines 510-538 of the same file), not on the decoded `len` value used inside `extractBytes32Array`/`extractBytesArray`/`extractSigArray`. This means an attacker can submit a small, cheap calldata payload (few hundred bytes) whose embedded length word claims billions of elements, and the allocation attempt happens before/without being gated by the energy already paid for the call.

### Impact Explanation
Any account able to send a signed transaction that triggers a `CALL`/`STATICCALL`/`DELEGATECALL` into the affected precompiled contract path (i.e., any TVM contract deployer or caller — no special privilege required) can supply calldata whose embedded "array length" word decodes to a value far larger than the actual calldata warrants. This forces the node executing/validating the transaction to attempt a multi-gigabyte array allocation, causing `OutOfMemoryError` on the validating full node/SR node. Because block validation and TVM execution happen synchronously as part of consensus-critical transaction processing, this can crash or destabilize nodes processing the malicious transaction, which is a node-crash/halt class impact analogous to the "overflow the server causing an OutOfMemory error" described in the Apache Mina SSHD advisory.

### Likelihood Explanation
Reaching this code only requires deploying or calling into a contract that invokes the specific TVM precompile(s) that route through these decoding helpers, and crafting one 32-byte calldata word with a value ≥ 2^32 (extremely simple to construct, e.g. all-`0xFF` bytes) at the position interpreted as an array/list length. No privileged role, no chain state manipulation, and no cooperation from other validators is needed — a single, ordinary broadcastable transaction from an unprivileged account is sufficient to reach and exercise the vulnerable code path. I was not able to fully confirm from available context which exact precompile(s) expose `extractBytes32Array` versus only `extractBytesArray`/`extractSigArray` at the outer `execute()` call sites, nor whether an outer-level `spendCount`/`receiveCount`-style cap (as seen at line ~1502 in the same file for the shielded-transaction path) is enforced before reaching every one of these helpers. This should be verified against the full call graph of `PrecompiledContracts.java` before treating every call site as equally exploitable.

### Recommendation
- In `extractBytes32Array`, `extractBytesArray`, and `extractSigArray`, validate the decoded `len` against the actual remaining size of `words`/`data` (e.g., `len * itemStride <= data.length - currentOffset`) before performing any allocation, and reject with a normal VM exception (energy-charged revert) rather than allowing an unbounded `new byte[len][...]`.
- Do not rely on `DataWord.intValueSafe()`'s silent clamp to `Integer.MAX_VALUE` as an implicit safety mechanism for allocation sizing; treat any clamped/out-of-range length as an immediate decode failure.
- Ensure energy/gas cost for these precompiles is computed (or a hard cap enforced) based on the decoded element count *before* allocation, not only on the raw `data.length`, so an attacker cannot get cheap-calldata/large-decoded-length asymmetry.

### Proof of Concept
1. Deploy a minimal contract that performs a `CALL`/`STATICCALL` to the precompiled contract address that internally uses `extractBytes32Array`/`extractBytesArray`/`extractSigArray` for its ABI-style array decoding.
2. Craft calldata where the word at the offset consumed as the "array length" is set to a value ≥ 2^32 (e.g., `0xFFFFFFFFFFFFFFFF...`), while the rest of the calldata is minimal/short.
3. Broadcast the transaction (or trigger the constant call via a full-node API) invoking this contract.
4. Observe that `words[offset].intValueSafe()` returns `Integer.MAX_VALUE` (per `DataWord.intValueSafe()` clamp behavior) and the subsequent `new byte[len][]` allocation attempts to allocate on the order of tens of gigabytes, producing `OutOfMemoryError` on the node executing the call — despite the cheap, small size of the actual submitted calldata.

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

**File:** common/src/main/java/org/tron/common/runtime/vm/DataWord.java (L219-229)
```java
  /**
   * In case of int overflow returns Integer.MAX_VALUE otherwise works as #intValue()
   */
  public int intValueSafe() {
    int bytesOccupied = bytesOccupied();
    int intValue = intValue();
    if (bytesOccupied > 4 || intValue < 0) {
      return Integer.MAX_VALUE;
    }
    return intValue;
  }
```
