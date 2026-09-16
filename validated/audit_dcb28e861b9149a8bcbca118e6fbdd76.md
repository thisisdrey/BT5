### Title
Unbounded Array Allocation from Attacker-Controlled Length Word in TVM Precompiled Signature Contracts (BatchValidateSign / ValidateMultiSign) - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
The `extractBytesArray`, `extractBytes32Array`, and `extractSigArray` helper methods in `PrecompiledContracts.java` read an array-length value directly from attacker-supplied EVM calldata (`words[offset].intValueSafe()`) and immediately use it to allocate a Java array (`new byte[len][]`) with no upper bound, unless a specific hard-fork flag (`VMConfig.allowTvmSelfdestructRestriction()`) is active. This mirrors the CVE-2025-54884 pattern: a length parameter taken directly from untrusted input is used to size a buffer/array without a sane cap, enabling memory-exhaustion DoS.

### Finding Description
`extractBytesArray` and `extractBytes32Array` compute `len` from a raw calldata word and allocate an array of that size before any validation: [1](#0-0) [2](#0-1) 

`DataWord.intValueSafe()` clamps an arbitrary 256-bit calldata word to `Integer.MAX_VALUE` rather than rejecting oversized values, so an attacker fully controls `len` up to ~2^31-1: [3](#0-2) 

These helpers are invoked from `ValidateMultiSign.execute` (precompile address `0x0a`) and `BatchValidateSign.doExecute` (precompile address `0x09`), which are reachable from any TVM `CALL`/`STATICCALL` to those fixed addresses: [4](#0-3) [5](#0-4) 

Critically, the size bound (`MAX_SIZE`) is only enforced when `VMConfig.allowTvmSelfdestructRestriction()` is true, and even then only for the signature/address arrays that are explicitly checked in that branch. When the flag is false (its legacy/pre-activation behavior), `extractBytesArray`/`extractBytes32Array` are called unconditionally with the raw, unbounded `len`, and `extractBytes32Array` (used for the `addresses` argument in `BatchValidateSign`) is called completely outside of any length gate in that code path — the `addrArraySize` guard only executes inside the `if (VMConfig.allowTvmSelfdestructRestriction())` block. This means the resource-allocation guard is entirely dependent on chain configuration/fork status rather than being an unconditional, defense-in-depth bound at the point of allocation.

### Impact Explanation
An attacker (unprivileged contract deployer / caller) can craft calldata where the length word preceding a `bytes[]`/`address[]` ABI parameter is a very large value (up to `Integer.MAX_VALUE`). This is forwarded to precompile `0x09` or `0x0a` via a `CALL` opcode from any deployed contract. `new byte[len][]` for `len` near `Integer.MAX_VALUE` attempts to allocate an array of that many object references (~8 bytes per slot on a 64-bit JVM, i.e., multi-gigabyte allocation attempts), which can throw `OutOfMemoryError` or otherwise exhaust heap/CPU on the node executing the transaction — a denial-of-service against any full node, SR, or validator that processes the transaction, potentially causing node crash or halted block processing. This matches the "node crash or halt" acceptance criterion.

### Likelihood Explanation
Reaching this code requires only: (1) deploying a small proxy/trampoline contract that performs a `CALL` to the fixed precompile address (`0x...09` or `0x...0a`) with attacker-controlled calldata, and (2) triggering that contract with a single transaction — both actions available to any unprivileged account with minimal TRX for fees/energy. Whether the vulnerable (unguarded) branch is reached depends on the current state of the `allowTvmSelfdestructRestriction` hard fork flag on the target chain; on chains/environments where this proposal has not yet been activated (e.g. private/consortium chains, or historical windows before mainnet activation), the unguarded path is directly reachable with no additional preconditions.

### Recommendation
Add an unconditional, sane upper bound check (independent of any fork-gate flag) on `len` immediately after reading it in `extractBytesArray`, `extractBytes32Array`, and `extractSigArray`, before performing `new byte[len][]`, returning an empty/failure result (or throwing a validation exception) if `len` exceeds the maximum expected item count (e.g., `MAX_SIZE` constants already defined in `ValidateMultiSign`/`BatchValidateSign`). Do not rely solely on a hard-fork flag to gate memory-safety-critical bounds checks — the check should be baked into the shared extraction helpers themselves.

### Proof of Concept
1. Deploy a minimal contract whose fallback/entry function does a raw `CALL` (or `STATICCALL`) to address `0x0000000000000000000000000000000000000000000000000000000000000009` (BatchValidateSign) or `...0a` (ValidateMultiSign), forwarding the caller-supplied calldata unmodified.
2. Send a transaction that triggers this contract with ABI-encoded calldata for `batchvalidatesign(bytes32,bytes[],address[])` where the length word for the `address[]` parameter (the word at the offset pointed to by `words[2]`) is set to a large value such as `0x7FFFFFFF` instead of a legitimate small count.
3. On a node where `allowTvmSelfdestructRestriction` is not yet activated, `extractBytes32Array` executes `new byte[0x7FFFFFFF][]`, attempting a multi-gigabyte allocation and triggering `OutOfMemoryError`/excessive GC pressure on the executing node, per: [6](#0-5)

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1051-1078)
```java
    @Override
    public Pair<Boolean, byte[]> execute(byte[] rawData) {
      if (VMConfig.allowTvmOsaka()
          && !isValidAbiEncoding(rawData, ABI_HEADER_WORDS, ABI_ITEM_WORDS)) {
        return Pair.of(false, EMPTY_BYTE_ARRAY);
      }
      DataWord[] words = DataWord.parseArray(rawData);
      byte[] address = words[0].toTronAddress();
      int permissionId = words[1].intValueSafe();
      byte[] data = words[2].getData();

      byte[] combine = ByteUtil.merge(address, ByteArray.fromInt(permissionId), data);
      byte[] hash = Sha256Hash.hash(CommonParameter
          .getInstance().isECKeyCryptoEngine(), combine);

      if (VMConfig.allowTvmSelfdestructRestriction()) {
        int sigArraySize = words[words[3].intValueSafe() / WORD_SIZE].intValueSafe();
        if (sigArraySize > MAX_SIZE) {
          return Pair.of(true, DATA_FALSE);
        }
      }
      byte[][] signatures = VMConfig.allowTvmSelfdestructRestriction() ?
          extractSigArray(words, words[3].intValueSafe() / WORD_SIZE, rawData) :
          extractBytesArray(words, words[3].intValueSafe() / WORD_SIZE, rawData);

      if (signatures.length == 0 || signatures.length > MAX_SIZE) {
        return Pair.of(true, DATA_FALSE);
      }
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1156-1181)
```java
    private Pair<Boolean, byte[]> doExecute(byte[] data)
        throws InterruptedException, ExecutionException {
      if (VMConfig.allowTvmOsaka()
          && !isValidAbiEncoding(data, ABI_HEADER_WORDS, ABI_ITEM_WORDS)) {
        return Pair.of(false, EMPTY_BYTE_ARRAY);
      }
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
      int cnt = signatures.length;
      if (cnt == 0 || cnt > MAX_SIZE || signatures.length != addresses.length) {
        return Pair.of(true, DATA_FALSE);
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
