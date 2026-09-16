### Title
Unbounded array-length read in `ValidateMultiSign` precompile signature extraction causes uncaught `ArrayIndexOutOfBoundsException` - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
The `ValidateMultiSign` precompiled contract (TVM address `0x0a`, reachable via a `CALL`/`STATICCALL` from any Solidity contract, i.e. from any unprivileged transaction that triggers a smart contract) extracts a signature array from raw call data using `extractBytesArray`/`extractSigArray` before entering any exception-guarded block. These helpers, like the unguarded `extractBytes32Array`, loop over an attacker-controlled `len` value read directly from call data without ever checking the loop index against `words.length`, so a crafted, correctly-shaped payload can drive the index past the end of the `words` array.

### Finding Description
`ValidateMultiSign.execute()` calls the extraction helpers immediately after parsing the input, outside of any `try { … } catch` block: [1](#0-0) 

The helper implementations bound-check only the initial `offset`, not the attacker-controlled `len` used to drive the loop: [2](#0-1) 

`len` comes straight from `words[offset].intValueSafe()`, an attacker-supplied 256-bit call-data word. Even when `VMConfig.allowTvmOsaka()` is active and `isValidAbiEncoding` gates the *overall byte length* of the payload: [3](#0-2) 
that check only validates that `data.length` is a multiple of the item size — it does not validate that the embedded "array length" words baked inside the ABI-encoded blob match the real number of items. A caller can therefore submit a data blob whose *total length* is a legal, correctly-shaped multiple of the item word size, but where the length word for the signature array itself (`words[offset]`) is a huge integer (e.g. `0x7fffffff`). This drives `words[offset + i + 1]` in `extractBytesArray`/`extractSigArray` far past the end of the `words` array, throwing an `ArrayIndexOutOfBoundsException` that is not caught anywhere inside `ValidateMultiSign.execute()`.

This is directly analogous to CVE-2017-9727: a length field taken from untrusted, attacker-controlled input is used to drive sequential reads without validating it against the real buffer size, producing an out-of-bounds read/crash.

Note: `BatchValidateSign.execute()` wraps its equivalent extraction logic (`doExecute`) in a `try { … } catch (Throwable t)` specifically to guard against this class of failure: [4](#0-3) 
`ValidateMultiSign` lacks the equivalent protection around its own extraction call site (lines 1072–1080), which is the root cause of the asymmetry.

### Impact Explanation
I was not able to confirm, within the available context, whether the TVM's `CALL`-to-precompile dispatch path (`Program.callToPrecompiledAddress`) generically catches `RuntimeException`/`ArrayIndexOutOfBoundsException` thrown by a precompiled contract's `execute()`, the way it appears to specifically catch `OutOfTimeException`/`InterruptedException` elsewhere. If such a generic catch does exist at that call site, the practical impact is limited to the specific transaction reverting/failing (a denial of service against the calling transaction only). If it does not exist, the uncaught exception would propagate out of transaction execution and could disrupt processing of the block/transaction (potentially halting the node or causing inconsistent transaction outcomes across nodes if handled differently in different code paths), which would be a High severity node-crash/availability issue. This uncertainty should be resolved by inspecting `Program.callToPrecompiledAddress` and the actuator-level exception handling around `TransactionTrace`/`Runtime` execution.

### Likelihood Explanation
The precompile is reachable by any account that can deploy or call a smart contract invoking address `0x0a` (`validatemultisign`) when `VMConfig.allowTvmSolidity059()` is enabled — a widely-enabled, non-privileged feature flag. Constructing calldata with an oversized embedded array-length word while keeping total data length correctly shaped is a trivial, deterministic crafting exercise requiring no special privileges, keys, or network position — a single crafted transaction is sufficient to trigger the code path.

### Recommendation
- Add an explicit bound check in `extractBytesArray`, `extractSigArray`, and `extractBytes32Array` so that `len` is rejected if `offset + len + 1 > words.length` (mirroring the existing `MAX_SIZE` checks already applied *after* extraction, but performed *before* the loop actually indexes into `words`).
- Wrap the extraction calls in `ValidateMultiSign.execute()` (lines 1057–1080) in the same defensive `try { … } catch (Throwable t)` pattern already used in `BatchValidateSign.execute()`, returning `Pair.of(true, DATA_FALSE)` on failure instead of allowing an exception to escape.
- Audit `Program.callToPrecompiledAddress` (and any other precompile invocation sites) to confirm a top-level catch exists for arbitrary `RuntimeException`s thrown by precompiled contract `execute()` implementations, to prevent any future precompile addition from having this same class of bug crash node execution.

### Proof of Concept
1. Enable `allowTvmSolidity059` (already enabled on mainnet/most networks).
2. Deploy any contract that performs a `STATICCALL`/`CALL` to precompile address `0x000000000000000000000000000000000000000000000000000000000000000a` (`validatemultisign(address,uint256,bytes,bytes[])`).
3. Craft the ABI-encoded call data so that:
   - The overall byte length satisfies `isValidAbiEncoding(data, 5, 5)` (a legal multiple of the item word size, so the TIP-854 guard does not reject it), and
   - The word at the signature-array offset (`words[3]`) points to a location whose "array length" value is a very large integer (e.g., `0x7fffffff`) while the actual trailing payload is short.
4. Invoke the contract with this crafted call data. `extractBytesArray`/`extractSigArray` will iterate `words[offset + i + 1]` far beyond `words.length`, throwing `ArrayIndexOutOfBoundsException` inside `ValidateMultiSign.execute()`, which is not caught locally — verify with a debugger/log that the exception is thrown at `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java:406` (or `:421` for the sig-array variant) before reaching the guarded permission-check block at line 1082.

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1057-1080)
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
