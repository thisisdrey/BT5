### Title
Unbounded/attacker-controlled memory allocation in TVM precompiled multisig contracts leads to node crash - ([File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java])

### Summary
The `ValidateMultiSign` (precompile `0x0a`) and `BatchValidateSign` (precompile `0x09`) TVM precompiled contracts decode ABI-encoded byte arrays using helper functions `extractBytesArray`, `extractBytes32Array`, and `extractSigArray`. These helpers allocate Java arrays sized directly from attacker-controlled 256-bit words returned by `DataWord.intValueSafe()`, with insufficient or missing upper-bound validation before the allocation occurs. This mirrors CVE-2018-10113's bug class: an untrusted length field drives an unbounded memory allocation that can crash the process on allocation failure.

### Finding Description
`DataWord.intValueSafe()` returns `Integer.MAX_VALUE` whenever the underlying 256-bit value occupies more than 4 bytes or is otherwise out of `int` range: [1](#0-0) 

`extractBytesArray`, `extractBytes32Array`, and `extractSigArray` use this value directly as an array-allocation size with no upper bound: [2](#0-1) 

In `extractBytesArray`, in addition to the outer array size (`len`), the **inner** per-item length (`bytesLen`, taken from `words[offset + bytesOffset + 1].intValueSafe()`) is passed unchecked into `extractBytes`, which calls `Arrays.copyOfRange(data, offset, offset + len)`. `Arrays.copyOfRange` allocates the destination array of size `to - from` *before* validating that the range fits inside the source array, so a single crafted 32-byte word can force allocation of an array up to ~2^31 bytes, independent of the actual size of `data`: [3](#0-2) [4](#0-3) 

These functions are reached from `ValidateMultiSign.execute()` and `BatchValidateSign.doExecute()`. A bound check (`sigArraySize > MAX_SIZE` / `addrArraySize > MAX_SIZE`) exists, but it is gated behind `VMConfig.allowTvmSelfdestructRestriction()` and only validates the **outer** array-count word — it never validates the **inner per-item length** word consumed by `extractBytesArray`'s `bytesLen`. When the legacy (unrestricted) code path is taken — i.e. `extractBytesArray` rather than `extractSigArray` — there is no bound at all on either the outer count or the inner per-item length before allocation: [5](#0-4) [6](#0-5) 

Crucially, the array allocation happens **before** the `signatures.length == 0 || signatures.length > MAX_SIZE` post-check and before any energy-based rejection meaningfully limits it, because `getEnergyForData` only charges energy proportional to the raw `data.length` (calldata size), not to the attacker-declared array-length words embedded within a small calldata payload: [7](#0-6) 

This is directly reachable from an unprivileged contract call: any account can invoke these precompiled addresses via a low-level `CALL`/`STATICCALL`, which is dispatched in `Program.callToPrecompiledAddress`: [8](#0-7) 

Neither `Program.callToPrecompiledAddress` nor `ValidateMultiSign.execute()` wraps the `extractBytesArray`/`extractBytes32Array` calls in a catch for `OutOfMemoryError` (the existing try/catch in `ValidateMultiSign.execute()` only wraps the *later* permission-checking logic, after the array is already allocated).

### Impact Explanation
A crafted transaction calling `ValidateMultiSign` or `BatchValidateSign` with small calldata but a maliciously large embedded "array length" 256-bit word can force the JVM to attempt allocating an array of up to ~2^31 elements/bytes. This throws `OutOfMemoryError`, which propagates uncaught through `Program.callToPrecompiledAddress` and up through transaction execution. Because every full node (not just witnesses) must execute the same transaction/contract call when validating or replaying a block, this can crash or destabilize any node that processes the transaction — a chain-wide denial-of-service vector triggerable by a single unprivileged transaction.

### Likelihood Explanation
Likelihood is high when the legacy/unguarded code path (`extractBytesArray`, taken when `VMConfig.allowTvmSelfdestructRestriction()` is not active) is exercised, since no validation exists at all. Even when that feature flag is active, the guard only checks the outer array-count word and does not validate the per-item `bytesLen` word consumed inside `extractBytes`, so the `Arrays.copyOfRange`-based unbounded allocation via a crafted inner length word remains reachable in `extractBytesArray`. Exploitation requires no special privileges — any account can send a contract call transaction targeting the precompile address with attacker-chosen calldata.

### Recommendation
- Validate every attacker-supplied length/offset word (`words[...].intValueSafe()`) against `MAX_SIZE`/`data.length` bounds **before** any array allocation, for both outer array counts and inner per-item lengths, in `extractBytesArray`, `extractBytes32Array`, and any callers.
- In `extractBytes`, explicitly check `offset + len <= data.length` and `len >= 0` and reasonably small before calling `Arrays.copyOfRange`, rather than relying on that method's own bounds checking (which allocates before validating).
- Remove the dependency on the `allowTvmSelfdestructRestriction()` feature flag for this specific bound check — it should be an unconditional invariant, not a conditionally-activated hardfork feature.
- Wrap precompile execution paths with a catch for `Throwable`/`OutOfMemoryError` at the `Program.callToPrecompiledAddress` boundary as defense in depth, in addition to fixing the root cause.

### Proof of Concept
Conceptual (not executed, no sandbox access): Craft a `CALL` to precompile address `0x0a` (`ValidateMultiSign`) with ABI-encoded calldata where:
- `words[3]` (the sig-array offset pointer) points to a small offset inside the calldata.
- The word at that offset (interpreted as the array "length") is set to a 256-bit value larger than 4 bytes, e.g. `0x0100000000000000000000000000000000000000000000000000000000000000` — causing `intValueSafe()` to return `Integer.MAX_VALUE`.
- With `VMConfig.allowTvmSelfdestructRestriction()` disabled (legacy path), `extractBytesArray` is called directly with `len = Integer.MAX_VALUE`, triggering `new byte[Integer.MAX_VALUE][]`, which throws `OutOfMemoryError` uncaught.

This can be validated conceptually against `BatchValidateSignContractTest`/`ValidateMultiSignContractTest`, which already construct calldata via `AbiUtil.parseParameters` for these exact method signatures, confirming the reachability of `words[...]` fields from transaction input: [9](#0-8) 

**Note on uncertainty**: I could not fully confirm within the available tool budget (a) the current default/mainnet-activated state of `VMConfig.allowTvmSelfdestructRestriction()` (whether the legacy unguarded `extractBytesArray` path is still reachable on production mainnet today), and (b) whether any additional upstream bounds checks exist elsewhere in the call chain that I did not locate. The `Arrays.copyOfRange` pre-allocation-before-bounds-check issue inside `extractBytesArray`'s inner `bytesLen`, however, appears to be independent of that flag and warrants verification/fix regardless.

### Citations

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L428-430)
```java
  private static byte[] extractBytes(byte[] data, int offset, int len) {
    return Arrays.copyOfRange(data, offset, offset + len);
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1044-1049)
```java
    @Override
    public long getEnergyForData(byte[] data) {
      long cnt = (data.length / WORD_SIZE - 5) / 5;
      // one sign 1500, half of ecrecover
      return cnt * ENGERYPERSIGN;
    }
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1052-1078)
```java
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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1156-1177)
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
```

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L1734-1774)
```java
    long requiredEnergy = contract.getEnergyForData(data);
    if (requiredEnergy > msg.getEnergy().longValue()) {
      // Not need to throw an exception, method caller needn't know that
      // regard as consumed the energy
      this.refundEnergy(0, CALL_PRE_COMPILED); //matches cpp logic
      this.stackPushZero();
    } else {
      // Delegate or not. if is delegated, we will use msg sender, otherwise use contract address
      if (msg.getOpCode() == Op.DELEGATECALL) {
        contract.setCallerAddress(getCallerAddress().toTronAddress());
      } else {
        contract.setCallerAddress(getContextAddress());
      }
      // this is the depositImpl, not contractState as above
      contract.setRepository(deposit);
      contract.setResult(this.result);
      contract.setConstantCall(isConstantCall());
      contract.setVmShouldEndInUs(getVmShouldEndInUs());
      Pair<Boolean, byte[]> out = contract.execute(data);

      if (out.getLeft()) { // success
        this.refundEnergy(msg.getEnergy().longValue() - requiredEnergy, CALL_PRE_COMPILED);
        this.stackPushOne();
        returnDataBuffer = out.getRight();
        deposit.commit();
      } else {
        // spend all energy on failure, push zero and revert state changes
        this.refundEnergy(0, CALL_PRE_COMPILED);
        this.stackPushZero();
        if (Objects.nonNull(this.result.getException())) {
          throw result.getException();
        }
      }

      if (VMConfig.allowTvmSelfdestructRestriction()) {
        this.memorySave(msg.getOutDataOffs().intValueSafe(), msg.getOutDataSize().intValueSafe(), out.getRight());
      } else {
        this.memorySave(msg.getOutDataOffs().intValue(), out.getRight());
      }
    }
  }
```

**File:** framework/src/test/java/org/tron/common/runtime/vm/BatchValidateSignContractTest.java (L215-226)
```java
  Pair<Boolean, byte[]> validateMultiSign(byte[] hash, List<Object> signatures,
      List<Object> addresses) {
    List<Object> parameters = Arrays.asList("0x" + Hex.toHexString(hash), signatures, addresses);
    byte[] input = Hex.decode(AbiUtil.parseParameters(METHOD_SIGN, parameters));
    contract.getEnergyForData(input);
    long maxExecutionTime = 2000; // ms
    contract.setVmShouldEndInUs(System.nanoTime() / 1000 + maxExecutionTime * 1000);
    Pair<Boolean, byte[]> ret = contract.execute(input);
    logger.info("BytesArray:{}，HexString:{}", Arrays.toString(ret.getValue()),
        Hex.toHexString(ret.getValue()));
    return ret;
  }
```
