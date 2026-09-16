### Title
Uncaught `ArrayIndexOutOfBoundsException` in `ValidateMultiSign` precompile from attacker-controlled ABI offset field - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
The `ValidateMultiSign` precompiled contract (invoked from any TVM contract via `CALL`) computes the signature-array index using an attacker-controlled offset word (`words[3]`) without validating it is within the bounds of the parsed `DataWord[] words` array before that lookup is performed. The `TIP-854` guard `isValidAbiEncoding` only checks the overall calldata length shape (`header + n*item` words), not that the embedded offset field actually points inside the array.

### Finding Description
In `ValidateMultiSign.execute()`: [1](#0-0) 

the line [2](#0-1) 
computes `words[words[3].intValueSafe() / WORD_SIZE]`. `words[3]` is a 32-byte value fully controlled by the calling contract's calldata; `isValidAbiEncoding` (used to gate this code path when `VMConfig.allowTvmOsaka()` is active) only verifies `data.length % 32 == 0` and that the tail length is a multiple of `itemWords*32`: [3](#0-2) 
It never checks that the offset word's decoded value actually addresses a valid index within `words`. An attacker can therefore submit calldata whose total length matches the expected `(5 + n*5)*32` shape (so the guard passes) but whose offset word (`words[3]`) is set to a huge value (e.g., near `0xFFFFFFFF`). `intValueSafe()` clamps this into a very large (but valid) `int`, and dividing by `WORD_SIZE` still yields an index far beyond `words.length`. Indexing `words[hugeIndex]` throws an uncaught `ArrayIndexOutOfBoundsException`.

This statement executes *before* the `try { ... } catch (Throwable t)` block that wraps the actual signature-recovery logic: [4](#0-3) 
so the exception is not contained by `ValidateMultiSign` itself, unlike the sibling `BatchValidateSign.execute()`, which wraps its entire `doExecute` call in `try { ... } catch (Throwable t)`: [5](#0-4) 

I was unable to fully confirm, within the available indexed content, whether `Program.callToPrecompiledAddress` (the call site in `actuator/src/main/java/org/tron/core/vm/program/Program.java`) wraps precompile `execute()` invocations in a blanket `catch (Throwable/RuntimeException)` that would convert this into a normal VM revert rather than letting the exception propagate out of transaction execution. The existing test suite (`OperationsTest.testTip854OuterFrameContainment`) only exercises the case where calldata is *too short* (caught by the `isValidAbiEncoding` length guard), not the case of well-formed-length calldata with an out-of-range offset word, so that specific attack path does not appear to be covered by regression tests.

### Impact Explanation
If uncaught exceptions from a precompile's `execute()` are not intercepted generically by `Program`/`Runtime`, then a single, unprivileged, attacker-crafted contract call to `ValidateMultiSign` (address `0x66` family) could throw an unhandled `ArrayIndexOutOfBoundsException` during transaction execution, potentially causing inconsistent handling or a node crash while processing the block that contains the transaction — a chain-halting condition if replicated non-deterministically, or at minimum a transaction/node-level denial of service. If, on the other hand, a higher-level catch-all in `Program`/`Runtime` does convert this into a revert, then the impact is limited to wasted energy for the caller rather than a node crash. Given the uncertainty about the top-level catch, I can only assert with confidence the bug-class match (attacker-controlled array index not bounds-checked before array access, similar in nature to the WebKit "improved bounds checks" CVE) — not the ultimate blast radius.

### Likelihood Explanation
The precompile is reachable by any account via a plain `CALL` from a deployed contract with fee-limited calldata; no special privilege, staking, or witness/committee status is required. Crafting the offset word to an out-of-range value while keeping the total length aligned to the expected header+item shape is trivial and requires no cryptographic material — only knowledge of the ABI layout, which is documented in the code/tests.

### Recommendation
Add explicit bounds checks before every `words[index]` access derived from an attacker-controlled offset field in `ValidateMultiSign.execute()` (and audit `extractBytes32Array`, which similarly lacks the `offset > words.length - 1` guard present in `extractSigArray`/`extractBytesArray`): [6](#0-5) 
Wrap the entire `ValidateMultiSign.execute()` body (not just the account-processing section) in a `try { ... } catch (Throwable t) { return Pair.of(true, DATA_FALSE); }`, mirroring `BatchValidateSign`'s containment pattern, and extend `isValidAbiEncoding` (or add a new check) to validate that decoded offset words actually resolve to indices within `words.length` before they are used to index the array.

### Proof of Concept
1. Deploy a contract that performs a low-level `call` to the `ValidateMultiSign` precompile address with `VMConfig.allowTvmSelfdestructRestriction()`/`allowTvmOsaka()` active.
2. Construct calldata of exactly `(5 + 1*5) * 32` bytes (passes `isValidAbiEncoding` with `ABI_HEADER_WORDS=5`, `ABI_ITEM_WORDS=5`), with:
   - `words[0]` = any 20-byte address (padded),
   - `words[1]` = any permission id,
   - `words[2]` = any hash word,
   - `words[3]` (the array offset field) = `0xFFFFFFFF...` (an out-of-range offset value),
   - remaining words = arbitrary padding to satisfy the length shape.
3. Call `execute(rawData)` — `words[words[3].intValueSafe() / WORD_SIZE]` at line 1067 computes an index far beyond `words.length`, throwing `ArrayIndexOutOfBoundsException` uncaught by any local `try/catch` in `ValidateMultiSign`.
4. Observe whether this exception propagates past `Program.callToPrecompiledAddress` uncaught (this final step requires direct inspection of `Program.java`'s precompile-call site, which I could not fully confirm from the indexed content — a Devin session with full repository access would be needed to trace the exact exception-handling chain up through `Runtime`/`Manager` block application).

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
