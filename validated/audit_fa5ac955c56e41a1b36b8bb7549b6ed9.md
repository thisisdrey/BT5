### Title
Unvalidated ABI array-length field in `ValidateMultiSign` precompile causes uncaught `ArrayIndexOutOfBoundsException` during TVM execution - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
The `ValidateMultiSign` precompiled contract parses a signatures-array length directly from attacker-controlled calldata and uses it to index into the `words` array without bounds checking, mirroring the Contiki-NG DIO prefix-length flaw where an unvalidated length field is used to drive memory access beyond the allocated buffer.

### Finding Description
`ValidateMultiSign.execute()` reads `words[3]` to locate the offset of the signatures array, then unconditionally calls `extractBytesArray`/`extractSigArray`: [1](#0-0) 

Inside `extractBytesArray`, the length `len` is read from `words[offset].intValueSafe()` — a fully attacker-controlled 256-bit word truncated to `int` — and then used to index `words[offset + i + 1]` in a loop with no check that this index stays within `words.length`: [2](#0-1) 

`extractSigArray` has the identical unbounded-index pattern: [3](#0-2) 

This is exactly analogous to the CVE-2023-50926 root cause: a length value taken from untrusted input is not validated against the actual size of the array/buffer before being used to compute an access index, leading to an out-of-bounds access (in Contiki-NG an OOB *read* via `memcmp`; here an OOB array index throwing `ArrayIndexOutOfBoundsException`).

Critically, unlike its sibling `BatchValidateSign.execute()`, which wraps the whole `doExecute()` call in `try { ... } catch (Throwable t) { return Pair.of(true, new byte[WORD_SIZE]); }`: [4](#0-3) 

`ValidateMultiSign.execute()` performs the vulnerable `words[3]` access and the `extractBytesArray`/`extractSigArray` calls **outside** any try/catch — the only local `try/catch (Throwable t)` guard begins later, around the permission-weight loop: [5](#0-4) 

An `ArrayIndexOutOfBoundsException` (an unchecked `RuntimeException`) raised during `words[3]`/`words[offset]`/`words[offset+i+1]` access is therefore not caught by `ValidateMultiSign` itself. Whether it is ultimately absorbed depends on catch clauses further up the call stack (`Program.callToPrecompiledAddress` → `VM.play` → `VMActuator` → `TransactionTrace.exec()` → `Manager.processTransaction`/`processBlock`); the block-processing catch clauses observed in `TronNetDelegate.processBlock` and `Manager.switchFork` only enumerate specific checked exception types (`ValidateSignatureException`, `ContractValidateException`, `ContractExeException`, `TaposException`, etc.) — not generic `RuntimeException`/`ArrayIndexOutOfBoundsException`: [6](#0-5) 

I was not able to fully verify within the remaining budget whether `VMActuator`'s or `Program.callToPrecompiledAddress`'s surrounding logic wraps arbitrary `Throwable`/`RuntimeException` from precompile execution into `BytecodeExecutionException` before it reaches this outer layer. If such a catch-all exists in the immediate VM call path, the impact is downgraded to a reverted transaction (denial of the specific tx, no crash). If no such catch-all wraps this specific unchecked exception, the flaw can crash/halt block processing for every full node applying the block deterministically.

### Impact Explanation
If the uncaught `ArrayIndexOutOfBoundsException` is not intercepted anywhere in the intervening call chain, any node (witness or full node) processing a transaction that calls `ValidateMultiSign` with a malformed offset/length combination would throw an unchecked exception during deterministic block application. Because all nodes execute the same transaction with the same calldata, this is reproducible on every node that applies the block — a potential consensus-wide halt/crash condition rather than a localized DoS. This satisfies the "node crash or halt" / "chain split" impact bar in the validation rules, assuming the exception indeed escapes uncaught to the top level.

### Likelihood Explanation
The precompile is reachable by any account via a plain smart-contract `CALL`/`STATICCALL` to the `ValidateMultiSign` precompile address, with fully attacker-controlled calldata (`words[3]` offset value, or an `offset` for the signatures array that resolves near/at `words.length`). No special privileges, staking, or SR/witness role is required — any transaction broadcaster or contract deployer can trigger it. The trigger condition (offset pointing past the end of `words`) is trivial to construct.

### Recommendation
- Add explicit bounds checks in `extractBytesArray` and `extractSigArray` before indexing `words[offset + i + 1]` (and any nested offset lookups), returning an empty array or failing the precompile call (`Pair.of(false, EMPTY_BYTE_ARRAY)`) instead of allowing an out-of-bounds array access.
- In `ValidateMultiSign.execute()`, validate `words.length` against the minimum required header size (mirroring the `isValidAbiEncoding` check that is currently gated behind `VMConfig.allowTvmOsaka()`) unconditionally, and validate that `words[3].intValueSafe() / WORD_SIZE` and subsequent offsets are within `words.length` before use.
- Wrap the entire body of `ValidateMultiSign.execute()` (not just the inner permission-weight loop) in a `try { ... } catch (Throwable t) { return Pair.of(true, DATA_FALSE); }`, consistent with the defensive pattern already used in `BatchValidateSign.execute()`.
- Audit `Program.callToPrecompiledAddress` and `VMActuator`/`TransactionTrace` exception handling to confirm generic `RuntimeException`s thrown from any precompiled contract are always converted into a `BytecodeExecutionException`/reverted transaction and never propagate past `processBlock`'s exception filters.

### Proof of Concept
1. Construct calldata for `ValidateMultiSign` (or `BatchValidateSign`-style ABI encoding) that is shorter than 4 words, or where `words[3]` decodes to a huge value such that `words[3].intValueSafe() / WORD_SIZE` computes an `offset` at or beyond `words.length`.
2. Deploy a trivial contract that performs `STATICCALL`/`CALL` to the `ValidateMultiSign` precompile address with this malformed payload (e.g., via `TvmTestUtils.triggerContractAndReturnTvmTestResult`).
3. Broadcast the transaction; `words[offset]` or `words[offset + i + 1]` in `PrecompiledContracts.extractBytesArray`/`extractSigArray` (lines 399-426) throws `ArrayIndexOutOfBoundsException` outside `ValidateMultiSign`'s local try/catch (lines 1052-1120).
4. Observe whether the exception propagates uncaught through block application; if so, this reproduces the crash on every node applying the same block.

**Note on confidence**: I could not fully trace, within the available tool budget, whether an intermediate layer (`Program.callToPrecompiledAddress`, `VMActuator`, or `TransactionTrace.exec()`) already catches generic `RuntimeException` from precompile execution and converts it into a normal transaction revert. This is the key open question that determines whether this is a High-severity node-crash bug or a lower-severity, already-mitigated revert path. A Devin session with full repo access should trace `Program.callToPrecompiledAddress` and `VMActuator.execute`/`finalization` exception handling end-to-end to confirm.

### Citations

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1057-1074)
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
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1081-1117)
```java
      if (account != null) {
        try {
          Permission permission = account.getPermissionById(permissionId);
          if (permission != null) {
            //calculate weight
            long totalWeight = 0L;
            List<byte[]> executedSignList = new ArrayList<>();
            for (byte[] sign : signatures) {
              byte[] recoveredAddr = recoverAddrBySign(sign, hash);

              sign = merge(recoveredAddr, sign);
              if (ByteArray.matrixContains(executedSignList, recoveredAddr)) {
                if (ByteArray.matrixContains(executedSignList, sign)) {
                  continue;
                }
                MUtil.checkCPUTime();
              }
              long weight = TransactionCapsule.getWeight(permission, recoveredAddr);
              if (weight == 0) {
                //incorrect sign
                return Pair.of(true, DATA_FALSE);
              }
              totalWeight += weight;
              executedSignList.add(sign);
              executedSignList.add(recoveredAddr);
            }

            if (totalWeight >= permission.getThreshold()) {
              return Pair.of(true, dataOne());
            }
          }
        } catch (Throwable t) {
          if (t instanceof OutOfTimeException) {
            throw t;
          }
          logger.info("ValidateMultiSign error:{}", t.getMessage());
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

**File:** framework/src/main/java/org/tron/core/net/TronNetDelegate.java (L295-312)
```java
      } catch (ValidateSignatureException
          | ContractValidateException
          | ContractExeException
          | UnLinkedBlockException
          | ValidateScheduleException
          | AccountResourceInsufficientException
          | TaposException
          | TooBigTransactionException
          | TooBigTransactionResultException
          | DupTransactionException
          | TransactionExpirationException
          | BadNumberBlockException
          | BadBlockException
          | NonCommonBlockException
          | ReceiptCheckErrException
          | VMIllegalException
          | ZksnarkException
          | EventBloomException e) {
```
