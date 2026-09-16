### Title
Uncaught `ArrayIndexOutOfBoundsException` in `ValidateMultiSign` precompile crashes/reverts a node transaction on malformed calldata - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
The `ValidateMultiSign` TVM precompiled contract (address `0x...0a`) parses raw call data into `DataWord[]` and immediately indexes into it (`words[0]`, `words[1]`, `words[2]`, `words[3]`) before any length validation is performed, unless the Osaka hardfork flag is active. Unlike the closely related `BatchValidateSign` precompile, which wraps its entire `doExecute` body in a `catch (Throwable t)` block, `ValidateMultiSign.execute()` only wraps the *later* permission-checking logic in a try/catch — the initial parsing and array indexing (lines 1057–1074) are unprotected.

### Finding Description
`ValidateMultiSign.execute()` is reachable by any account issuing a `CALL`/`STATICCALL`/`DELEGATECALL` TVM opcode to precompile address `0x0a` (e.g. from a deployed smart contract, or a raw `TriggerSmartContract` transaction with the precompile as the target). [1](#0-0) 

When `VMConfig.allowTvmOsaka()` is disabled (or in general prior to that hardfork's `isValidAbiEncoding` guard), `DataWord.parseArray(rawData)` is invoked and the resulting array is indexed with no bounds checking: [2](#0-1) 

If `rawData` is empty, too short, or crafted so that `words[3].intValueSafe() / WORD_SIZE` points past the end of the array, this throws an unchecked `ArrayIndexOutOfBoundsException` before the code ever reaches the `try { ... } catch (Throwable t)` block that starts at line 1082 — that block only guards the subsequent permission/signature-weight logic, not the parsing step: [3](#0-2) 

By contrast, the sibling `BatchValidateSign` precompile defends against exactly this class of malformed input by catching `Throwable` around its entire execution and degrading gracefully to a zeroed result: [4](#0-3) 

This asymmetry is the root cause: `ValidateMultiSign` lacks the same defensive wrapper as `BatchValidateSign`, allowing an unchecked exception to propagate up through `Program.callToPrecompiledAddress` (`contract.execute(data)` is called with no surrounding try/catch there either): [5](#0-4) 

This mirrors the PaddlePaddle `paddle.put_along_axis` bug class (CWE-476: reachable, insufficiently-validated input leads to an unguarded pointer/array dereference and process-level failure), except the java-tron analog manifests as an uncaught runtime exception from insufficient bounds validation rather than a native nullptr segfault.

### Impact Explanation
Any account can trigger this by calling the precompile with truncated/empty input data through a smart contract or directly via `TriggerSmartContract`. Because the exception is unchecked and uncaught at the point of failure, it propagates out of `execute()`; whether this manifests as a per-transaction failure (denial of service against the caller and inconsistent node behavior relative to other implementations) or as a broader disruption depends on whether an outer generic-exception handler exists further up the transaction/block-processing stack — this repository index did not let me trace all the way to `Manager`/`TransactionTrace` to confirm whether this exception is caught generically there. At minimum, this is an availability/consensus-consistency issue: nodes could diverge in whether/how they handle the malformed call, and the missing symmetry with `BatchValidateSign`'s explicit `Throwable` handling strongly suggests this was an intentional defense that was omitted here by mistake.

### Likelihood Explanation
High reachability: the precompile is called via a standard TVM opcode with attacker-fully-controlled calldata, requiring no special privileges, asset ownership, or witness/SR status — just a deployed contract or direct `TriggerSmartContract` call.

### Recommendation
Add the same `isValidAbiEncoding`-style validation and/or wrap the entire `ValidateMultiSign.execute()` body (including the initial `DataWord.parseArray` and array-indexing steps) in a `try/catch (Throwable t)` identical to what `BatchValidateSign.execute()` already does, returning `Pair.of(true, DATA_FALSE)` or `Pair.of(false, EMPTY_BYTE_ARRAY)` on any parsing failure instead of allowing the exception to escape uncaught.

### Proof of Concept
1. Deploy or call a contract that issues a `STATICCALL`/`CALL` to precompile address `0x000000000000000000000000000000000000000000000000000000000000000a` with `rawData` shorter than 4 words (e.g., empty bytes) while `allowTvmOsaka` is not active.
2. `DataWord.parseArray(rawData)` returns a `DataWord[]` shorter than 4 elements.
3. The subsequent `words[0].toTronAddress()` / `words[1].intValueSafe()` / `words[3].intValueSafe()` accesses throw `ArrayIndexOutOfBoundsException`, which is not caught anywhere within `execute()` prior to that point, unlike the equivalent `BatchValidateSign` path. [2](#0-1)

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1051-1077)
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
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1080-1119)
```java
      AccountCapsule account = this.getDeposit().getAccount(address);
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
      }
      return Pair.of(true, DATA_FALSE);
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

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L1748-1756)
```java
      contract.setRepository(deposit);
      contract.setResult(this.result);
      contract.setConstantCall(isConstantCall());
      contract.setVmShouldEndInUs(getVmShouldEndInUs());
      Pair<Boolean, byte[]> out = contract.execute(data);

      if (out.getLeft()) { // success
        this.refundEnergy(msg.getEnergy().longValue() - requiredEnergy, CALL_PRE_COMPILED);
        this.stackPushOne();
```
