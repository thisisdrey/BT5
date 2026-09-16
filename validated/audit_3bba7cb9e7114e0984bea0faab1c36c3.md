### Title
Unchecked array index in TVM `ValidateMultiSign` precompiled contract allows unauthenticated call-data-triggered `ArrayIndexOutOfBoundsException` - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
`PrecompiledContracts.ValidateMultiSign.execute(byte[] rawData)` parses attacker-controlled `rawData` into a `DataWord[]` array and immediately indexes fixed offsets (`words[0]`, `words[1]`, `words[2]`, `words[3]`) before any length/shape validation is guaranteed to have run. The only bounds check present (`isValidAbiEncoding`) is gated behind `VMConfig.allowTvmOsaka()`, so on chains/forks where that flag is not active, a short `rawData` payload (fewer than 4 words) causes `DataWord.parseArray` to return a short array, and the subsequent `words[1]`/`words[2]`/`words[3]` accesses throw `ArrayIndexOutOfBoundsException` outside of any try/catch in that code path. This mirrors the vLLM bug class (CWE-129): a count/length is assumed present and indexed into without validating the backing structure actually has that many entries.

### Finding Description
```java
// actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java
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
  ...
  if (VMConfig.allowTvmSelfdestructRestriction()) {
    int sigArraySize = words[words[3].intValueSafe() / WORD_SIZE].intValueSafe();
    ...
``` [1](#0-0) 

Key points:
- The `isValidAbiEncoding` bounds/shape check is only applied `if (VMConfig.allowTvmOsaka())`. When that fork flag is not enabled, `words = DataWord.parseArray(rawData)` can produce an array shorter than 4 elements for any `rawData` smaller than 128 bytes, yet the code unconditionally accesses `words[0]` through `words[3]`.
- This is directly analogous to the vLLM report's root cause: a value (`video_grid_thw`/`image_grid_thw` in vLLM, `words` array here) is indexed based on an assumed shape/count without validating that the underlying structure actually contains that many entries, causing an unhandled index-out-of-range exception.
- The `try { ... } catch (Throwable t) { ... }` block in this method only wraps the signature-recovery/weight-calculation logic that begins at `AccountCapsule account = this.getDeposit().getAccount(address);`. It does **not** wrap the earlier `words[0]`/`words[1]`/`words[2]`/`words[3]` accesses, so an `ArrayIndexOutOfBoundsException` thrown there is not caught locally. [2](#0-1) 

Reachability: `ValidateMultiSign` is a precompiled contract reachable from any TVM contract execution (`TriggerSmartContract`/`CreateSmartContract`), i.e. any unprivileged account can deploy or call a contract that performs a low-level `call`/`staticcall` to this precompile's address with attacker-chosen `rawData`. This is the same class of "single crafted transaction/contract call reaches unguarded array indexing" pattern that the report calls out for vLLM's `_vl_get_input_positions_tensor`.

### Impact Explanation
Because the offending indexing happens outside of the local `try/catch(Throwable)`, the exception propagates up through the TVM opcode dispatch loop. The interpreter loop in `VM.play` does catch generic `RuntimeException` (which `ArrayIndexOutOfBoundsException` is a subclass of) at the per-opcode level and converts it into a stopped/failed program execution rather than crashing the node process: [3](#0-2) 

Given this generic catch exists in the opcode execution loop, the most likely concrete effect is a failed/aborted transaction execution (denial of that specific contract call, energy consumed) rather than a full node crash — this is a materially weaker impact than the vLLM report's remote worker-crash DoS, because java-tron's VM execution loop appears to already contain a defensive `catch (RuntimeException e)` around every opcode dispatch that the vLLM code lacked. I was unable to fully trace, within the available tool budget, every call path from `Program`'s CALL-family opcodes into `PrecompiledContracts.execute` to confirm with certainty that no code path exists where this exception could escape the VM's exception handling and hit an outer uncaught-exception handler (e.g., during transaction validation/estimation paths that call precompiled contracts outside the normal VM opcode loop, such as constant/static calls or the `TransactionUtil`/energy estimation flows). This uncertainty should be verified with a live reproduction.

### Likelihood Explanation
High reachability (any account can trigger it via a contract call with short call data to the precompile address), but the practical severity is bounded by the existing `catch (RuntimeException e)` in `VM.play`, which strongly suggests this results in a normal failed-execution/revert rather than a node-crashing DoS. Confirming node-crash severity would require either (a) finding a call path to this precompile that bypasses `VM.play`'s exception handling, or (b) live reproduction against a running node.

### Recommendation
- Validate `rawData.length` unconditionally (not only when `VMConfig.allowTvmOsaka()`) before indexing `words[0..3]`, e.g. require `rawData != null && rawData.length >= ABI_HEADER_WORDS * WORD_SIZE` and return `Pair.of(false, EMPTY_BYTE_ARRAY)` on failure, mirroring the pattern already used in `extractBytesArray`/`extractSigArray` (`if (offset > words.length - 1) return new byte[0][];`). [4](#0-3) 
- Move the bounds check for `words[3]`-derived offset (`words[words[3].intValueSafe() / WORD_SIZE]`) before use regardless of `allowTvmSelfdestructRestriction`.
- Widen the `try { } catch (Throwable t)` in `ValidateMultiSign.execute` to cover the full body, including the initial `words[...]` accesses, so any parsing failure degrades gracefully to `Pair.of(true, DATA_FALSE)` instead of throwing.
- Add regression tests specifically calling `ValidateMultiSign` with `rawData` shorter than 4 words with `allowTvmOsaka` disabled, to confirm no uncaught exception escapes.

### Proof of Concept
Conceptual PoC (requires live-node verification, which was not performed):
1. Deploy a minimal Solidity contract that performs `staticcall`/`call` to the `ValidateMultiSign` precompile address with `rawData` of length less than 128 bytes (fewer than 4 32-byte words), e.g. call with just 32 bytes of data.
2. With `allowTvmOsaka` not activated on the target fork, `isValidAbiEncoding` check is skipped, so `DataWord.parseArray(rawData)` returns a `DataWord[]` of length 1.
3. `words[1]` access throws `ArrayIndexOutOfBoundsException`.
4. Observe whether the transaction merely reverts/fails (expected, given `VM.play`'s catch) or whether any unhandled path exists that could destabilize the node — this final step needs to be confirmed on an actual java-tron devnet, which could not be done in this analysis session.

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

**File:** actuator/src/main/java/org/tron/core/vm/VM.java (L93-103)
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
```
