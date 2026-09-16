## Title
Out-of-bounds array read via attacker-controlled length field in TVM precompiled contract signature-array parsing — (`File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
The GStreamer `gst_avi_demux_riff_parse_vprp()` bug computes an entry count from an attacker-controlled field and iterates that many times without validating that each entry actually fits inside the remaining buffer, causing out-of-bounds reads. The same bug class exists in java-tron's `extractSigArray`, `extractBytesArray` and `extractBytes32Array` helpers used by the `ValidateMultiSign` and `BatchValidateSign` TVM precompiled contracts: the number of signatures/addresses to read (`len`) is taken directly from attacker-supplied calldata and used as a loop bound to index into the `DataWord[] words` array, without ever checking that `offset + i + 1` stays within `words.length`.

### Finding Description
`extractSigArray`/`extractBytesArray`/`extractBytes32Array` only bound-check the initial `offset` argument (`if (offset > words.length - 1) return new byte[0][];`), then read the untrusted count from that single word and loop over it indexing further into the array with no per-iteration bounds check: [1](#0-0) 

`ValidateMultiSign.execute()` calls this parsing code with a caller-controlled dynamic offset (`words[3]`) before any exception handling is in scope — the extraction happens outside the local `try { ... } catch (Throwable t)` block that only wraps the signature-recovery loop: [2](#0-1) 

The only structural guard is `isValidAbiEncoding`, gated behind `VMConfig.allowTvmOsaka()`, which merely checks that `data.length` is congruent with `headerWords + n*itemWords` — it does **not** validate that the `len` value read from an attacker-chosen offset, or the derived index `offset + i + 1`, actually stays inside the parsed `words` array: [3](#0-2) 

This mirrors the vprp defect exactly: a count value taken from untrusted input is used to bound a read loop instead of being validated against the true remaining buffer/array size.

`BatchValidateSign` has been hardened by wrapping the entire `doExecute` body — including the array extraction — in a `try/catch (Throwable)`: [4](#0-3) 

but `ValidateMultiSign.execute()` has no such wrapper around the `extractSigArray`/`extractBytesArray` call, so an `ArrayIndexOutOfBoundsException` (or, for very large `len` values, an `OutOfMemoryError` from `new byte[len][]`) thrown during parsing propagates directly out of `execute()`, uncaught.

### Impact Explanation
Any account can reach this by calling `address(0x...ValidateMultiSignAddr).call(data)` from any smart contract or `TriggerSmartContract` transaction, since precompiled contracts are dispatched from `OperationActions.exeCall` → `Program.callToPrecompiledAddress` for every `CALL/CALLCODE/DELEGATECALL/STATICCALL`: [5](#0-4) [6](#0-5) 

The resulting uncaught exception ultimately unwinds to `VMActuator.execute()`'s top-level `catch (Throwable e)`, which spends all remaining energy and marks the transaction as failed rather than crashing the node: [7](#0-6) 

The confirmed impact is therefore a deterministic transaction failure with full energy consumption (denial-of-service against the caller's own transaction), and — for a large enough length value chosen to trigger an oversized `new byte[len][]` allocation before the array-index check is reached — a risk of `OutOfMemoryError` that stresses the whole JVM heap during block/transaction execution on every full node processing that transaction.

### Likelihood Explanation
Low complexity, no privilege required: a single crafted `TriggerSmartContract` (or a call from any deployed contract) with a malformed dynamic-offset/length field to the `ValidateMultiSign` precompile address triggers the defect. `BatchValidateSign`'s analogous path is already defended by a blanket `try/catch`, but `ValidateMultiSign` is not, making it the concretely exploitable variant.

### Recommendation
In `extractSigArray`, `extractBytesArray`, and `extractBytes32Array`, validate `len` and every derived index (`offset + i + 1`, `bytesOffset + offset + 1/2`) against `words.length` (and against `data.length` for byte-copy operations) before use, returning an empty/failure result instead of throwing. Additionally, wrap `ValidateMultiSign.execute()`'s calldata-parsing section in the same defensive `try/catch (Throwable)` pattern already applied to `BatchValidateSign.execute()`/`doExecute()`.

### Proof of Concept
1. Deploy a contract that performs `address(VALIDATE_MULTI_SIGN_ADDR).call(payload)`.
2. Craft `payload` so that `data.length` satisfies `isValidAbiEncoding` (header=5 words + n*5 words), but the dynamic offset word (`words[3]`) points to a `len` value such that `offset + len + 1` exceeds `words.length` (e.g., set the length word at the offset location to a large value like `0xFFFFFFFF`).
3. Invoke the contract via `TriggerSmartContract`; `extractSigArray`/`extractBytesArray` throws `ArrayIndexOutOfBoundsException` (or attempts a huge `new byte[len][]` allocation) before entering `ValidateMultiSign`'s local `try/catch`, propagating an uncaught exception out of `execute()`.

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1057-1078)
```java
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

**File:** actuator/src/main/java/org/tron/core/vm/OperationActions.java (L1046-1056)
```java
    PrecompiledContracts.PrecompiledContract contract =
        PrecompiledContracts.getContractForAddress(codeAddress);
    if (contract != null) {
      if (program.isConstantCall()) {
        contract =  PrecompiledContracts.getOptimizedContractForConstant(contract);
      }
      program.callToPrecompiledAddress(msg, contract);
    } else {
      program.callToAddress(msg);
    }
    program.step();
```

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L1752-1766)
```java
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
```

**File:** actuator/src/main/java/org/tron/core/actuator/VMActuator.java (L287-301)
```java
    } catch (Throwable e) {
      if (!(e instanceof TransferException)) {
        program.spendAllEnergy();
      }
      result = program.getResult();
      result.rejectInternalTransactions();
      clearExceptionResult(result);
      if (Objects.isNull(result.getException())) {
        logger.error(e.getMessage(), e);
        result.setException(new RuntimeException("Unknown Throwable"));
      }
      if (StringUtils.isEmpty(result.getRuntimeError())) {
        result.setRuntimeError(result.getException().getMessage());
      }
      logger.info("runtime result is :{}", result.getException().getMessage());
```
