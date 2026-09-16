### Title
Unbounded attacker-controlled array-length in `PrecompiledContracts` decode helpers causes an uncaught `OutOfMemoryError` and node crash - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
`ValidateMultiSign` and `BatchValidateSign` are TVM precompiled contracts reachable by any account via a normal `CALL`/`STATICCALL` from a deployed smart contract (no special privilege required). Their calldata is decoded with `extractBytesArray`, `extractSigArray` and `extractBytes32Array`, which read an attacker-supplied 32-byte "array length" word directly out of the raw calldata and immediately use it to allocate a Java array (`new byte[len][]`) with no upper bound check on `len` itself.

### Finding Description
`extractBytesArray` reads the length purely from calldata content, only guarding that the *offset* itself is inside the `words` array, not that `len` is sane: [1](#0-0) 

`extractBytes32Array` has no bound check at all on the offset or the length: [2](#0-1) 

These helpers are invoked from `ValidateMultiSign.execute` and `BatchValidateSign.doExecute`, using length/offset words taken straight from the calldata (`words[3]`, `words[1]`, `words[2]`) before any bounds-sane validation of the *value itself* (only `words[3].intValueSafe()/WORD_SIZE` position is bounded, not the magnitude of the array-length word it decodes): [3](#0-2) [4](#0-3) 

The recently added TIP-854 guard (`isValidAbiEncoding`) only validates that the *total* calldata length is a multiple of the expected head/item word size; it does not validate that the internal length-pointer fields decoded by `extractBytesArray`/`extractBytes32Array` are consistent with the real payload size: [5](#0-4) 

Because `len` can be crafted to be a large positive `int` (up to `Integer.MAX_VALUE`), `new byte[len][]` (an array of object references) or `new byte[len][]` in `extractBytes32Array` attempts to allocate gigabytes of memory, throwing `java.lang.OutOfMemoryError`. Crucially, `OutOfMemoryError` extends `java.lang.Error`, not `RuntimeException`. The TVM interpreter loop in `VM.play` only catches `RuntimeException`, `JVMStackOverFlowException`/`OutOfTimeException`, and `StackOverflowError` — it has no handler for a generic `OutOfMemoryError`: [6](#0-5) 

The precompile call path (`callToPrecompiledAddress`) also does not wrap `contract.execute(data)` in anything but the caller's exception model, so the `Error` propagates out of the contract-execution stack entirely: [7](#0-6) 

An uncaught `Error` escaping transaction execution during block application is not the intentional TVM failure/revert path (which is `RuntimeException`-based); it can propagate up through `TransactionTrace`/`Runtime`/`Manager` block-application code and crash or terminate the node process (or corrupt its running state), unlike a normal reverted transaction.

### Impact Explanation
Any unprivileged account that deploys a trivial contract calling the `ValidateMultiSign` (`0x0...66`-style) or `BatchValidateSign` precompile address with crafted calldata can trigger a multi-gigabyte allocation attempt inside the interpreter thread that is applying the block. This is a denial-of-service against the node: `OutOfMemoryError` is not caught by the VM's exception handling, so it can abort block processing / crash the JVM, halting the node — one of the explicitly in-scope impact categories ("node crash or halt").

### Likelihood Explanation
High reachability: this only requires deploying a contract and issuing one transaction that performs a `CALL`/`STATICCALL` to the `ValidateMultiSign`/`BatchValidateSign` precompile address with attacker-chosen calldata bytes — no special role (SR/witness/committee) is needed, and the length field is fully attacker controlled independent of the TIP-854 total-length guard.

### Recommendation
- In `extractBytesArray`, `extractSigArray`, and `extractBytes32Array`, clamp/validate the decoded `len` against a sane upper bound (e.g. `MAX_SIZE` constants already used by `ValidateMultiSign`/`BatchValidateSign`) *before* allocating any array, and validate `len` is non-negative and consistent with the remaining `words.length`/`data.length`.
- Reject with `Pair.of(false, EMPTY_BYTE_ARRAY)` (or the existing `DATA_FALSE` short-circuit) instead of allocating, whenever the decoded length exceeds the precompile's `MAX_SIZE` or does not fit the available calldata.
- Consider adding a defensive catch for `Throwable`/`OutOfMemoryError` at the VM/precompile-call boundary as defense-in-depth, in addition to fixing the root-cause validation.

### Proof of Concept
1. Deploy a minimal contract with a fallback/function that performs `staticcall`/`call` to the `ValidateMultiSign` precompile address, passing raw calldata such that:
   - `words[3]` points to an offset within bounds of the `words` array (to pass the `offset > words.length - 1` guard in `extractBytesArray`), but
   - the word at that offset (used as `len`) is set to a very large value, e.g. `0x7fffffff`.
2. Broadcast the transaction; when `Program.callToPrecompiledAddress` invokes `contract.execute(data)`, `extractBytesArray`/`extractSigArray` executes `new byte[len][]` with `len` ≈ 2^31, causing the JVM to attempt to allocate on the order of many gigabytes, raising `OutOfMemoryError`.
3. Because `VM.play`'s catch blocks only match `RuntimeException`/`StackOverflowError`/timeouts, the `Error` is not converted into a normal TVM revert and instead escapes the interpreter, disrupting block application on the node that processes this transaction.

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1051-1074)
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
