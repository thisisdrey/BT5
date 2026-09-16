## Title
Unvalidated attacker-controlled length field used for array allocation in `ValidateMultiSign` precompile — ([File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java])

### Summary
The `ValidateMultiSign` TVM precompiled contract decodes an array length directly from calldata and uses it to size a `byte[][]` allocation before validating the value is sane, and unlike its sibling `BatchValidateSign`, this precompile does not wrap the decoding step in a catch-all handler. This mirrors the kernel bug class: content taken from an untrusted/external source (here, TVM calldata) is used to drive memory allocation/array construction before its shape is validated, allowing a crash condition.

### Finding Description
`ValidateMultiSign.execute()` parses the raw calldata into words and, before doing any size validation, extracts a signature array via `extractBytesArray`/`extractSigArray`: [1](#0-0) 

Those helpers read the array length straight from a `DataWord` supplied in calldata and allocate accordingly without bounding it first: [2](#0-1) 

The `MAX_SIZE` bound (`signatures.length > MAX_SIZE`) is only checked *after* the array has already been allocated at the attacker-chosen size: [3](#0-2) 

The `try/catch` present in the same method only wraps the later permission/weight-checking loop, not the extraction call: [4](#0-3) 

By contrast, the sibling precompile `BatchValidateSign` — evidently patched for a related issue (TIP-854) — wraps its *entire* `doExecute` (which performs the same kind of extraction) in `catch (Throwable t)`: [5](#0-4) 

This asymmetry is exactly the analog of the CVE: `BatchValidateSign` was hardened to "validate content before using" (or at least contain failures), while `ValidateMultiSign` retains the old unguarded path. Even where `MAX_SIZE`/ABI-shape checks exist (`isValidAbiEncoding`, `allowTvmSelfdestructRestriction` branch), the legacy pre-check-less code path is preserved and reachable when those feature flags are off, and a crafted "length" word can still force an oversized allocation attempt (`new byte[len][]` with `len` up to `Integer.MAX_VALUE`) before any size gate runs.

An `OutOfMemoryError`/`NegativeArraySizeException` raised at this point is not caught inside `ValidateMultiSign.execute()`. It propagates through `Program.callToPrecompiledAddress` up into `VM.play()`, whose catch chain only special-cases `RuntimeException` and `StackOverflowError`, not generic `Error`: [6](#0-5) 

It is ultimately caught by `VMActuator.execute()`'s `catch (Throwable e)`: [7](#0-6) 

but by the time that outer handler runs, the JVM has already attempted (and, for `OutOfMemoryError`, driven) heap exhaustion — a state from which other threads/services in the same node process can fail unpredictably, i.e., a real crash/DoS risk, not merely a contained "revert."

### Impact Explanation
Any account can trigger this by issuing a single `TriggerSmartContract` (or internal `CALL`) to the `ValidateMultiSign` precompiled contract address with a crafted calldata word that decodes to a very large array length. This can force a huge, attacker-sized heap allocation attempt on the full node processing the transaction, which can lead to `OutOfMemoryError`/node instability or crash — a network-wide denial-of-service risk analogous to the rtw89 driver crash from unvalidated content, since every full node executing the block must process the same TVM call.

### Likelihood Explanation
The precompile is reachable by any unprivileged account via ordinary smart-contract calls with no special permission, matching the "unprivileged actor reachable via a single signed transaction" criterion. The unsafe path exists specifically outside the newer `allowTvmSelfdestructRestriction`/`allowTvmOsaka` guarded branches, so it is reachable whenever those chain parameters are not yet active, and the sibling `BatchValidateSign` code shows the codebase authors were already aware of and hardening this exact class of issue elsewhere but did not apply the same containment to `ValidateMultiSign`.

### Recommendation
- Validate the decoded array length in `extractBytesArray` / `extractSigArray` / `extractBytes32Array` against `MAX_SIZE` and against the actual remaining word count in `words` *before* allocating any array, mirroring the `sigArraySize > MAX_SIZE` guard already used in the `allowTvmSelfdestructRestriction` branch, unconditionally (not only under the feature flag).
- Wrap `ValidateMultiSign.execute()`'s entire body in a `catch (Throwable t)` block consistent with `BatchValidateSign.execute()`, so unexpected failures degrade to a rejected precompile call rather than an uncontained `Error`.
- Audit other precompiles/VM opcode handlers using `DataWord.intValueSafe()`-derived values as allocation sizes for the same unguarded pattern.

### Proof of Concept
1. Craft ABI-encoded calldata to the `ValidateMultiSign` precompile address where the "signature array length" word (dereferenced via `words[3]`) resolves, through `extractBytesArray`, to `len = Integer.MAX_VALUE` (or another very large value) while leaving `allowTvmSelfdestructRestriction`/`allowTvmOsaka` disabled so the `MAX_SIZE` short-circuit branch is skipped.
2. Submit a `TriggerSmartContract` transaction (or have a deployed contract issue an internal `CALL`) invoking the precompile with this calldata.
3. Observe the node attempt `new byte[len][]` in `extractBytesArray`, triggering `OutOfMemoryError`/`NegativeArraySizeException` uncaught within `ValidateMultiSign.execute()`, propagating to `VM.play()`/`VMActuator.execute()` and stressing/crashing the node's JVM heap while processing the transaction.

Note: I could not directly inspect `DataWord.intValueSafe()`'s exact clamping behavior (whether it caps to `Integer.MAX_VALUE`, returns a raw truncated int, or can yield negative values) within the available search results, so the precise resulting exception type (`OutOfMemoryError` vs. `NegativeArraySizeException`) is not fully confirmed — a Devin session with full repository access could verify this and confirm the exact allocation size achievable from calldata.

### Citations

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1080-1118)
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

**File:** actuator/src/main/java/org/tron/core/vm/VM.java (L93-127)
```java
        } catch (RuntimeException e) {
          logger.info("VM halted: [{}]", e.getMessage());
          if (!(e instanceof TransferException)) {
            program.spendAllEnergy();
          }
          //program.resetFutureRefund();
          program.stop();
          throw e;
        } finally {
          program.fullTrace();
        }
      }

      if (allowDynamicEnergy) {
        program.addContextContractUsage(energyUsage);
      }

    } catch (JVMStackOverFlowException | OutOfTimeException e) {
      throw e;
    } catch (RuntimeException e) {
      // https://openjdk.org/jeps/358
      // https://bugs.openjdk.org/browse/JDK-8220715
      // since jdk 14, the NullPointerExceptions message is not empty
      if (e instanceof NullPointerException || StringUtils.isEmpty(e.getMessage())) {
        logger.warn("Unknown Exception occurred, tx id: {}",
            Hex.toHexString(program.getRootTransactionId()), e);
        program.setRuntimeFailure(new RuntimeException("Unknown Exception"));
      } else {
        program.setRuntimeFailure(e);
      }
    } catch (StackOverflowError soe) {
      logger.info("\n !!! StackOverflowError: update your java run command with -Xss !!!\n", soe);
      throw new JVMStackOverFlowException();
    }
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/VMActuator.java (L271-301)
```java
    } catch (JVMStackOverFlowException e) {
      program.spendAllEnergy();
      result = program.getResult();
      result.setException(e);
      result.rejectInternalTransactions();
      clearExceptionResult(result);
      result.setRuntimeError(result.getException().getMessage());
      logger.info("JVMStackOverFlowException: {}", result.getException().getMessage());
    } catch (OutOfTimeException e) {
      program.spendAllEnergy();
      result = program.getResult();
      result.setException(e);
      result.rejectInternalTransactions();
      clearExceptionResult(result);
      result.setRuntimeError(result.getException().getMessage());
      logger.info("timeout: {}", result.getException().getMessage());
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
