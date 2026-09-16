### Title
Unbounded/uncaught array-index heap read in `ValidateMultiSign.execute` via attacker-controlled offset word - ([File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java])

### Summary
CVE-2019-9773 is a heap-based buffer overflow in `dwg_decode_eed_data` caused by trusting an attacker-controlled size/offset field without validating it against the buffer bounds. The same bug class exists in java-tron's `ValidateMultiSign` precompiled contract (address `0x...0a`), where an attacker-controlled ABI "offset" word is used directly to index into a `DataWord[]` array with no bounds check and no surrounding exception handling.

### Finding Description
`ValidateMultiSign.execute` decodes the raw call data into `DataWord[] words = DataWord.parseArray(rawData)` and then, when `VMConfig.allowTvmSelfdestructRestriction()` is enabled, computes an array index directly from attacker-supplied call data: [1](#0-0) 

`words[3].intValueSafe() / WORD_SIZE` is an unvalidated offset taken straight from the transaction/contract-call input. It is used to index `words[...]` (`int sigArraySize = words[words[3].intValueSafe() / WORD_SIZE].intValueSafe();`) and is then passed into `extractSigArray`/`extractBytesArray`. Compare this to the sibling helper `extractBytes32Array`, which performs no bounds check at all before reading `words[offset]` and `words[offset + i + 1]`: [2](#0-1) 

The only structural guard, `isValidAbiEncoding`, merely checks that the overall calldata length is 32-byte aligned and that the trailing region is a multiple of the per-item word count — it never validates that offset fields point within the bounds of `words`: [3](#0-2) 

Critically, unlike `BatchValidateSign.execute`, which wraps its entire `doExecute` call in a `try { } catch (Throwable t)` that swallows any exception and returns a safe default: [4](#0-3) 

`ValidateMultiSign.execute` has **no such outer catch**. The `try/catch(Throwable t)` block inside it only wraps the account-permission-checking logic, not the earlier code that computes `words[words[3].intValueSafe() / WORD_SIZE]`: [5](#0-4) 

An attacker who crafts a `validatemultisign(address,uint256,bytes32,bytes[])` call with a `bytes[]` "offset" word (`words[3]`) pointing far outside the decoded `words` array size will cause an uncaught `ArrayIndexOutOfBoundsException` to propagate directly out of `execute()`.

### Impact Explanation
An uncaught runtime exception from a precompiled contract's `execute()` is not the expected control-flow contract of `PrecompiledContract` (`abstract Pair<Boolean, byte[]> execute(byte[] data)`), and the surrounding TVM `CALL` dispatch logic is only confirmed to defensively catch exceptions for `BatchValidateSign`/`ValidateMultiSign` in the newer TIP-854 test suite for the *structural* validation path (`isValidAbiEncoding`), not for out-of-bounds offsets that still pass that structural check. If the TVM's call dispatcher (`Program`) does not uniformly catch arbitrary `RuntimeException`/`Error` from every precompile, this can crash transaction execution for the node processing the block/transaction — a potential node-crash / chain-halt condition reachable by any account issuing a plain TRC-10/TRX-funded contract call to precompile address `0x0a`, with no special privileges required.

I was not able to fully confirm within the available context whether `Program.callToPrecompiledAddress` has a blanket `catch (Throwable/RuntimeException)` around all precompile `execute()` invocations (the grep for that pattern in `Program.java` returned matches but I could not review their exact placement relative to the precompile call before running out of tool budget). If such a catch-all exists at the `Program` level, the practical impact is downgraded to a poisoned/incorrect execution result rather than a crash. This uncertainty should be resolved by a follow-up review of `Program.java`'s precompile invocation path.

### Likelihood Explanation
High reachability: `ValidateMultiSign` is a standard precompiled contract reachable from any smart contract via a `CALL`/`STATICCALL` to address `0x...0a`, callable by any unprivileged transaction sender who can trigger contract execution (e.g., a wrapper contract that forwards attacker-supplied `bytes` to the precompile, or directly via `TriggerSmartContract`). No special permissions, SR/witness role, or additional preconditions are needed beyond crafting the ABI-encoded input, which is entirely under the caller's control.

### Recommendation
- Add explicit bounds validation before every `words[...]` array access derived from attacker-controlled offset/length fields in `ValidateMultiSign.execute`, `extractBytes32Array`, `extractBytesArray`, and `extractSigArray` (mirroring the `if (offset > words.length - 1) return new byte[0][];` guard already present in `extractBytesArray` but missing in `extractBytes32Array`).
- Wrap the entirety of `ValidateMultiSign.execute` (not just the inner permission-check block) in a `try { } catch (Throwable t)` that returns `Pair.of(true, DATA_FALSE)` on any decoding failure, consistent with `BatchValidateSign.doExecute`'s error-handling pattern.
- Extend `isValidAbiEncoding` (or add a dedicated check) to validate that all offset words used for indexing (`words[3]`, `words[1]`, `words[2]`, etc.) resolve to indices within `words.length` before they are dereferenced.

### Proof of Concept
Craft a call to `validatemultisign(address,uint256,bytes32,bytes[])` where the ABI-encoded offset for the `bytes[]` parameter (the value at `words[3]`) is set to a large value (e.g., `0xFFFFFFFF` scaled such that `words[3].intValueSafe() / WORD_SIZE` exceeds `words.length`), while keeping total calldata length 32-byte aligned so it passes `isValidAbiEncoding`. Enable `allowTvmSelfdestructRestriction`. Submitting this as calldata to a contract that forwards it to precompile address `0x...0a` via `CALL` triggers `words[words[3].intValueSafe() / WORD_SIZE]` in `ValidateMultiSign.execute`, throwing an uncaught `ArrayIndexOutOfBoundsException` before the existing inner `try/catch` block is reached.

**Caveat on confidence**: due to index/tool-call limits, I could not fully trace whether `Program.java`'s precompile dispatch already catches this exception generically (which would reduce this from a potential node-crash to a contained execution-revert issue). This should be verified before treating the "node crash" impact as fully confirmed; the out-of-bounds/unbounded-index root cause itself, however, is confirmed by direct code inspection.

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1051-1080)
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
      }

      AccountCapsule account = this.getDeposit().getAccount(address);
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
