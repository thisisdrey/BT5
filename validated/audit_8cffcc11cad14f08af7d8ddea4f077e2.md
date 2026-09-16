Based on the analysis, I found a solid analog in the TVM precompiled contracts.

### Title
DoS via uncaught ArrayIndexOutOfBoundsException from unchecked index computation in ValidateMultiSign precompile - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
`PrecompiledContracts.ValidateMultiSign.execute()` computes an index into the `words` array derived directly from attacker-controlled call data (`words[3]`) and uses it to index the same array before any bounds validation is guaranteed to run, mirroring the OpenEXR bug class of indexing a collection using an unchecked, attacker-influenced value derived from the input itself.

### Finding Description
`ValidateMultiSign.execute(byte[] rawData)` parses call data into a `DataWord[] words` array and then computes `words[3].intValueSafe() / WORD_SIZE` as an index used to read `words[...]` a second time to derive `sigArraySize`, and again to call `extractSigArray`/`extractBytesArray`: [1](#0-0) 

This bounds check is only performed when `VMConfig.allowTvmSelfdestructRestriction()` is enabled, and even then only checks `sigArraySize > MAX_SIZE`, not that the index `words[3].intValueSafe() / WORD_SIZE` itself is within `words.length`. The earlier `isValidAbiEncoding` guard is likewise conditioned on `VMConfig.allowTvmOsaka()`: [2](#0-1) 

Critically, this raw array-indexing code executes *before* the `try { ... } catch (Throwable t)` block that only wraps the later permission/weight-calculation logic: [3](#0-2) 

This is structurally inconsistent with the sibling `BatchValidateSign` precompile, whose entire body is wrapped by a `try { return doExecute(data); } catch (Throwable t) { ... }` at the outer `execute()` method, explicitly catching any indexing exception and returning a safe failure result instead of propagating it: [4](#0-3) 

`ValidateMultiSign` has no equivalent outer catch-all around its index computation, so a call with crafted `words[3]` value pointing past the end of the parsed `words` array (e.g., an oversized offset relative to `rawData.length / WORD_SIZE`) throws an uncaught `ArrayIndexOutOfBoundsException` directly out of `execute()`.

### Impact Explanation
Any account can trigger this by deploying a contract (or building a raw call) that issues a `STATICCALL`/`CALL` to the `ValidateMultiSign` precompiled contract address (`0x...a`) with malformed calldata where the computed offset word exceeds the parsed `words` array bounds. Because the exception is uncaught within the precompile itself, whether it results in a benign reverted transaction or an uncaught exception that escalates during transaction execution/block application depends on how far up the call stack (Program/Runtime/Manager) it is finally caught — this final propagation point could not be fully confirmed within the available tool budget, so the ultimate blast radius (single tx revert vs. broader execution/block-application disruption) is uncertain and should be verified against `Program.callToPrecompiledAddress` and the surrounding `TransactionTrace`/`Runtime` exception handling.

### Likelihood Explanation
High: the precompile is reachable by any unprivileged contract deployer/caller via a single transaction; no special permission or chain configuration state is required except that TVM be operational (this precompile has been active since the `validateMultiSign` opcode was enabled). The only condition is malformed calldata, which is trivial to construct deterministically.

### Recommendation
Add an explicit bounds check (e.g., `words[3].intValueSafe() / WORD_SIZE < words.length`) before indexing `words[...]` for `sigArraySize`, and wrap the entire `ValidateMultiSign.execute()` body (not just the later part) in the same `try { ... } catch (Throwable t) { return safe-failure }` pattern already used in `BatchValidateSign.execute()`, so malformed input degrades gracefully to a rejected/failed precompile call instead of raising an uncaught exception.

### Proof of Concept
1. Construct calldata for a `CALL`/`STATICCALL` to the `ValidateMultiSign` precompiled address (`000...a`) with:
   - `words[0]` = arbitrary 21-byte-padded address
   - `words[1]` = arbitrary permission id
   - `words[2]` = an offset word pointing within bounds (for the `data` bytes parameter)
   - `words[3]` = a large offset value such that `words[3].intValueSafe() / WORD_SIZE` computed in `PrecompiledContracts.java:1067` exceeds the total number of parsed words in `rawData`.
2. Send this as a transaction calling a contract that issues the crafted call to the precompile address.
3. `words[words[3].intValueSafe() / WORD_SIZE]` at line 1067 throws `ArrayIndexOutOfBoundsException`, uncaught within `ValidateMultiSign.execute()` (unlike `BatchValidateSign`, which is fully guarded). [1](#0-0)

### Citations

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1144-1181)
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
