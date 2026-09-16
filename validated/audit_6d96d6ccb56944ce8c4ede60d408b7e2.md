### Title
Unbounded array-length allocation in `ValidateMultiSign`/`BatchValidateSign` precompiles allows an OutOfMemoryError node crash - ([File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java])

### Summary
The `ValidateMultiSign` and `BatchValidateSign` TVM precompiled contracts decode an attacker-controlled length word from raw calldata and pass it directly into an array allocation (`new byte[len][]`) with no upper-bound check, when the `allowTvmSelfdestructRestriction` chain parameter is not active. `DataWord.intValueSafe()` clamps any oversized/overflowing value to `Integer.MAX_VALUE` rather than rejecting it, so a single crafted contract call can force an attempted allocation of a `Integer.MAX_VALUE`-length array. This mirrors the reported bug class: an attacker-controlled length computation is used to size a memory operation without validating it against the actual available/backing data, causing an unrecoverable crash.

### Finding Description
`extractBytesArray` and `extractBytes32Array` read a length word straight from the raw precompile input and allocate an array of that size before doing any bound validation against the input size: [1](#0-0) 

`intValueSafe()` never throws for oversized values — it silently clamps to `Integer.MAX_VALUE`: [2](#0-1) 

In `ValidateMultiSign.execute()`, the size guard (`sigArraySize > MAX_SIZE`) is only evaluated when `VMConfig.allowTvmSelfdestructRestriction()` is enabled; when it is disabled, `extractBytesArray` is called unguarded with the raw `len`: [3](#0-2) 

The same unguarded pattern exists in `BatchValidateSign.doExecute()`: [4](#0-3) 

If the resulting allocation throws `OutOfMemoryError`, that is a JVM `Error`, not a `RuntimeException`. The VM opcode-execution loop only catches `RuntimeException` (and a couple of named exceptions) around per-opcode execution and precompile calls; `OutOfMemoryError` is not one of the handled types: [5](#0-4) 

An uncaught `OutOfMemoryError` propagating out of transaction execution during block application is not a normal, per-transaction failure — it can destabilize or crash the whole node process, since the JVM heap state after an `OutOfMemoryError` is often inconsistent for other threads too.

### Impact Explanation
`allowTvmSelfdestructRestriction` is a chain parameter toggled by an SR proposal (`ProposalUtil.java`), so its value is network/time dependent — it is not guaranteed to be active on every chain (mainnet history, private/side chains, or forks that have not enabled it). On any network where it is inactive, any unprivileged account can call the `ValidateMultiSign` (address `0x66`) or `BatchValidateSign` (address `0x67`) precompile — directly via a `CALL` opcode from a smart contract, reachable from a single signed, ordinary transaction — with a crafted length word to attempt a multi-gigabyte array allocation. This can crash the node process serving RPC/API requests or, more critically, a validator applying the block, resulting in a denial-of-service / node-crash condition, matching the "node crash or halt" acceptance criterion.

### Likelihood Explanation
The precompile is reachable by any user who can submit a `TriggerSmartContract` transaction that performs a `CALL` to the fixed precompile addresses — no special permission or witness/committee role is needed. The only precondition is that `allowTvmSelfdestructRestriction` is not enabled on the target network, which I could not confirm/deny for the "in-scope" production network from the available index (its activation state is set via governance proposals and stored in `DynamicPropertiesStore`, not a static default I could verify here). This uncertainty tempers the assessed likelihood: if the flag is permanently enabled and unable to be disabled on the current production chain, the exposure is closed there, but the code path itself remains present and reachable when the flag is off.

### Recommendation
- Bound-check the decoded `len` value in `extractBytesArray` / `extractBytes32Array` / `extractSigArray` against the actual size of `data`/`words` (e.g., reject if `len` is negative, exceeds `MAX_SIZE`, or exceeds `(words.length - offset - 1)`), independent of the `allowTvmSelfdestructRestriction` feature flag, so the fix applies unconditionally rather than only in the "restricted" code path.
- Avoid relying on `intValueSafe()`'s silent clamp-to-`Integer.MAX_VALUE` semantics for any value used to size an allocation.

### Proof of Concept
1. On a network/snapshot where `allowTvmSelfdestructRestriction` is disabled, deploy a contract that performs a low-level `CALL` to precompile address `0x0000...66` (ValidateMultiSign) with ABI-encoded input where the signature-array length word (at the offset referenced by `words[3]`) is set to `0xFFFFFFFF` (or any value whose decoded `intValueSafe()` clamps to `Integer.MAX_VALUE`).
2. Broadcast a normal `TriggerSmartContract` transaction invoking this call from any unprivileged account.
3. During execution, `extractBytesArray` executes `new byte[Integer.MAX_VALUE][]`, attempting to allocate roughly 8–16 GB of reference storage, throwing `OutOfMemoryError`.
4. Because `VM.play()`'s opcode-execution try/catch only handles `RuntimeException`/`StackOverflowError`/named timeout exceptions, the `OutOfMemoryError` is not caught at this layer and can propagate into block-application code, risking a node crash.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L390-404)
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
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1066-1074)
```java
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

**File:** actuator/src/main/java/org/tron/core/vm/VM.java (L93-126)
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
```
