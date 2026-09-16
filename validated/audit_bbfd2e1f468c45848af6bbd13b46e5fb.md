### Title
Attacker-controlled array length in TVM `ValidateMultiSign`/`BatchValidateSign` precompiles causes unbounded memory allocation and node crash - ([File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java])

### Summary
`PrecompiledContracts.ValidateMultiSign.execute()` and `PrecompiledContracts.BatchValidateSign.doExecute()` decode a `bytes[]` (signatures) and `address[]` array from raw call data by trusting an attacker-supplied length word taken straight from the calldata (`words[offset].intValueSafe()`), and use that value to size a Java array (`new byte[len][]`) before any sane upper bound is enforced. This mirrors CWE-770 in the Cosign advisory: allocation size is derived directly from untrusted input.

### Finding Description
The helper functions in `PrecompiledContracts.java` compute the array length from the raw TVM call data with no bound check other than what the caller may or may not perform first: [1](#0-0) 

- `extractBytes32Array` reads `len = words[offset].intValueSafe()` and immediately does `new byte[len][]` with **no bound check at all**.
- `extractBytesArray`/`extractSigArray` also read `len` the same way; the only guard is `offset > words.length - 1`, which does not limit `len` itself.

`DataWord.intValueSafe()` returns `Integer.MAX_VALUE` whenever the 256-bit word does not fit safely into an `int` (i.e., whenever the attacker sets a large calldata word): [2](#0-1) 

This means an attacker can force `len` up to `Integer.MAX_VALUE` (~2.1 billion), causing `new byte[len][]` (or `new byte[len]`) to attempt to allocate an object-reference array of billions of slots.

Reachability:
- `BatchValidateSign.doExecute()` calls `extractBytes32Array(words, words[2].intValueSafe() / WORD_SIZE)` for the `addresses` array **unconditionally**, regardless of the `VMConfig.allowTvmSelfdestructRestriction()` feature switch — only the *signatures* extraction path is guarded by that flag, and even then, only when the flag is enabled: [3](#0-2) 

- `ValidateMultiSign.execute()` calls `extractSigArray`/`extractBytesArray` for signatures the same way, and the size check (`sigArraySize > MAX_SIZE`) is also gated behind `VMConfig.allowTvmSelfdestructRestriction()`: [4](#0-3) 

Both precompiles are reachable from any smart contract deployed by an unprivileged account via a plain `CALL`/`STATICCALL` to the `validatemultisign`/`batchvalidatesign` precompiled addresses — i.e., from any `TriggerSmartContract` or `TriggerConstantContract` transaction/API request, with no special privileges required.

Crucially, in `ValidateMultiSign.execute()` the array extraction happens **before** the only local `try { ... } catch (Throwable t)` block (which wraps only the signature-verification loop, not the extraction call). An `OutOfMemoryError` thrown while allocating the oversized array is therefore not contained inside the precompile method at all, and propagates up through `Program.callToPrecompiledAddress` → `VMActuator` → `TransactionTrace.exec()` → `Manager.processTransaction`/block application. While `VMActuator.create/call` executor does have a generic `catch (Throwable e)` at the top level of contract execution, that only catches the error after the JVM has already attempted and possibly triggered severe GC pressure/allocation failure, and does not prevent memory exhaustion affecting the whole node process (all threads share the same heap).

### Impact Explanation
An attacker can craft the ABI-encoded parameter of `validatemultisign(address,uint256,bytes32,bytes[])` or `batchvalidatesign(bytes32,bytes[],address[])` so that the "array length" word encoded in calldata is an extremely large value (near `Integer.MAX_VALUE`). Executing this via any `TriggerSmartContract`/`TriggerConstantContract` call (broadcastable by anyone, or reachable via `/wallet/triggerconstantcontract` JSON-RPC/HTTP query paths) causes the node to attempt an allocation of an array with billions of elements. This can:
- Throw `OutOfMemoryError`, forcing costly full garbage collection cycles across the whole JVM heap shared by block validation, other pending transactions, and the HTTP/gRPC/JSON-RPC API layer — a machine-wide denial of service, consistent with the Cosign advisory's impact description.
- If encountered during block application (a witness signing or validating this transaction inside a block), it can stall or crash the node's block-processing thread, potentially impacting chain progress on the affected node.
- If exercised repeatedly via constant/estimate-energy calls (read-only queries), it can degrade or crash the query-serving node without even needing an on-chain transaction.

This qualifies as a "node crash or halt"/"API the node can no longer serve" impact under the scan's acceptance criteria.

### Likelihood Explanation
Likelihood is high for the `BatchValidateSign` `addresses` extraction path (`extractBytes32Array`), since it is called unconditionally with no length bound irrespective of the `allowTvmSelfdestructRestriction` committee-controlled feature flag. For the signatures extraction paths in both precompiles, exploitability depends on whether `VMConfig.allowTvmSelfdestructRestriction()` is active on the target network; if disabled (e.g., on forks, private/permissioned chains, or testnets that have not activated this TIP), the vulnerability is fully exploitable for signatures as well. No special permissions, high value, or complex conditions are required — a single crafted transaction/contract call from any account suffices.

### Recommendation
- In `extractBytes32Array`, `extractBytesArray`, and `extractSigArray`, validate `len` against a small sane maximum (e.g., the existing `MAX_SIZE` constants used by `ValidateMultiSign`/`BatchValidateSign`) **before** allocating any array, regardless of the `allowTvmSelfdestructRestriction` flag state.
- Make the `MAX_SIZE` bound check in `BatchValidateSign.doExecute()` unconditional (not gated by `VMConfig.allowTvmSelfdestructRestriction()`), and perform it before calling `extractBytes32Array`/`extractBytesArray`.
- Wrap the array-extraction calls in `ValidateMultiSign.execute()` within the existing exception-containment boundary so that unexpected `Error`/`Exception` conditions cannot escape the precompile and destabilize the calling transaction/block-processing thread.

### Proof of Concept
1. Deploy a trivial contract that performs `staticcall`/`call` to the `batchValidateSign` precompiled address (`0x...09` per `PrecompiledContracts` address table) with ABI-encoded parameters for `batchvalidatesign(bytes32,bytes[],address[])`, where the `address[]` length-word offset is crafted so that the word read as the array count equals a very large value (e.g., `0xFFFFFFFF...` such that `DataWord.intValueSafe()` returns `Integer.MAX_VALUE`).
2. Broadcast a `TriggerSmartContract` (or send a `TriggerConstantContract` query via the HTTP/JSON-RPC API) invoking this contract function.
3. Observe `PrecompiledContracts.extractBytes32Array` attempt `new byte[Integer.MAX_VALUE][]`, causing `OutOfMemoryError`/heavy GC thrashing in the executing node's JVM, independent of whether `allowTvmSelfdestructRestriction` is enabled.
4. Repeat via `ValidateMultiSign`'s `bytes[]` signatures parameter (`validatemultisign(address,uint256,bytes32,bytes[])`) when `allowTvmSelfdestructRestriction` is disabled, observing the uncaught allocation failure propagate out of `ValidateMultiSign.execute()`.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L390-426)
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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1156-1182)
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
      byte[] res = new byte[WORD_SIZE];
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
