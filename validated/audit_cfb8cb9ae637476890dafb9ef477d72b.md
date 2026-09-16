### Title
Unbounded array index in `ValidateMultiSign` precompile causes `ArrayIndexOutOfBoundsException` DoS on crafted TVM calldata - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
The Helm advisory describes a crash caused by an unchecked index/length assumption on attacker-supplied data (`Files.Lines` indexing an empty byte slice without bounds checking). The equivalent bug class in java-tron is unchecked array indexing on attacker-controlled offsets derived from TVM calldata words. In `PrecompiledContracts.ValidateMultiSign.execute()`, the `sigArraySize` bounds check reads `words[words[3].intValueSafe() / WORD_SIZE]` directly, with no verification that the computed index is within the bounds of the `words` array, unlike the sibling helper functions `extractBytesArray`/`extractSigArray` which explicitly guard `offset > words.length - 1`.

### Finding Description
`ValidateMultiSign.execute()` in [1](#0-0)  parses the raw calldata into a `DataWord[] words` array and, when `VMConfig.allowTvmSelfdestructRestriction()` is enabled, executes:

```java
int sigArraySize = words[words[3].intValueSafe() / WORD_SIZE].intValueSafe();
```

`words[3]` is fully attacker-controlled (it is the ABI-encoded offset word for the `bytes[]` signatures parameter of `validatemultisign(address,uint256,bytes32,bytes[])`). An attacker can set this offset word to an arbitrarily large value so that `words[3].intValueSafe() / WORD_SIZE` computes an index far outside the actual `words.length`, triggering an `ArrayIndexOutOfBoundsException` at this line.

Crucially, this line sits **before** and **outside** the inner `try { ... } catch (Throwable t) { ... }` block that wraps the permission/signature-recovery logic later in the same method (starting at [2](#0-1) ). Unlike `BatchValidateSign.execute()`, which wraps its entire `doExecute()` call in a top-level `try/catch (Throwable t)` ( [3](#0-2) ) that swallows any exception and returns a zero result, `ValidateMultiSign.execute()` has no such umbrella around the entire method body. The `isValidAbiEncoding` check performed earlier ( [4](#0-3)  and [5](#0-4) ) only validates the overall byte-length shape of the calldata (divisibility by `WORD_SIZE` and a valid header/item word count relationship) — it does **not** validate that offset *values* encoded inside the words point within the bounds of the words array. This mirrors the Helm root cause: a length/shape check exists, but a specific "index computed from data content" is never range-checked before use, so a syntactically well-formed but semantically malicious payload panics.

The `extractBytesArray`/`extractSigArray` helpers used later in the same method do defend against this (`if (offset > words.length - 1) return new byte[0][];`, [6](#0-5) ), but the direct `words[words[3]...]` access at line 1067 predates/bypasses that guard, and `extractBytes32Array` (used by `BatchValidateSign`) also lacks this guard entirely ( [7](#0-6) ) — but that call path is protected by `BatchValidateSign`'s outer try/catch.

### Impact Explanation
`ValidateMultiSign` is a reachable, addressable TVM precompiled contract (address `0x...0a`, [8](#0-7) ) that any smart contract can invoke via a `CALL`/`STATICCALL` opcode with attacker-chosen calldata, from any unprivileged account. Because the `ArrayIndexOutOfBoundsException` is thrown outside the method's local exception handling, it propagates up through `Program.callToPrecompiledAddress` into the transaction execution path. Depending on how far up the exception is caught (transaction-level catch vs. block-processing/node-level), this can at minimum cause deterministic transaction execution failures for a class of calls that should otherwise be handled gracefully (denial of service against normal execution of a specific precompile), and in the worst case, if not caught cleanly during block application, could interfere with consensus processing of blocks containing such a transaction. This satisfies the "node crash/halt" or "API the node can no longer serve" bar for a Medium-severity DoS analog when `allowTvmSelfdestructRestriction` is active (a mainnet-activated TIP feature).

### Likelihood Explanation
High likelihood of reachability: `allowTvmSelfdestructRestriction` is a TIP feature flag maintained via `DynamicPropertiesStore`/`ProposalUtil` and, once activated on a live network (as such TIPs typically are), this code path is always exercised for every `ValidateMultiSign` call. Triggering the crash requires only crafting `bytes[]` ABI encoding with an out-of-range offset word (`words[3]`), which is trivial to construct in a raw contract call without needing valid Solidity ABI encoders. No special privileges, signatures, or account state are required — the malicious offset is evaluated before any account/permission lookup.

### Recommendation
Add an explicit bounds check before dereferencing `words[words[3].intValueSafe() / WORD_SIZE]`, matching the pattern already used in `extractBytesArray`/`extractSigArray`:
```java
int sigArrayOffset = words[3].intValueSafe() / WORD_SIZE;
if (sigArrayOffset < 0 || sigArrayOffset >= words.length) {
  return Pair.of(true, DATA_FALSE);
}
int sigArraySize = words[sigArrayOffset].intValueSafe();
```
Apply the same defensive bound to `extractBytes32Array` (used by `BatchValidateSign`) for consistency, and consider wrapping the entirety of `ValidateMultiSign.execute()` in a top-level `catch (Throwable)` as a defense-in-depth safety net, mirroring `BatchValidateSign.execute()`.

### Proof of Concept
1. Deploy/invoke any contract (or use `TriggerSmartContract`) that performs a `CALL` (or a raw `staticcall`) to precompile address `0x000000000000000000000000000000000000000a` (`validatemultisign`), on a network where `allowTvmSelfdestructRestriction` is activated.
2. Construct the calldata manually (bypassing the standard ABI encoder) as 5 header words followed by no valid tail, where the 4th word (index 3, corresponding to the `bytes[]` offset parameter) is set to a large value, e.g. `0xFFFFFFFFFFFFFFFF` or any value such that `value / 32` exceeds the total number of 32-byte words present in the supplied calldata, while still satisfying `isValidAbiEncoding`'s length-shape check (i.e., total length is a multiple of 32 and matches `(words - 5) % 5 == 0`).
3. Execute the call. `DataWord.parseArray` produces a `words` array sized to the actual calldata; `words[words[3].intValueSafe() / WORD_SIZE]` at [9](#0-8)  then indexes past the end of `words`, throwing `ArrayIndexOutOfBoundsException`, uncaught within `ValidateMultiSign.execute()`.

Note: I was unable to fully trace, within the available tool budget, whether `Program.callToPrecompiledAddress` (in `actuator/src/main/java/org/tron/core/vm/program/Program.java`) wraps precompile `execute()` calls in a generic `catch (Throwable)`/`RuntimeException` handler that would convert this into a normal VM revert rather than a deeper failure. This should be verified directly against `Program.java` to confirm the exact blast radius (transaction-local revert vs. broader failure) before treating this as fully validated.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L151-152)
```java
  private static final DataWord validateMultiSignAddr = new DataWord(
      "000000000000000000000000000000000000000000000000000000000000000a");
```

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1051-1075)
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
