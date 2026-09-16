### Title
Unbounded permission-signature offset parsing causes uncaught index exception in `ValidateMultiSign` precompile - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
The `ValidateMultiSign` precompiled contract (TVM opcode `validatemultisign`, invoked via a `STATICCALL`/`CALL` from any deployed contract by an unprivileged transaction sender) parses attacker-controlled call data into a `DataWord[]` and immediately indexes into it using offsets taken directly from that data, before any bounds validation or a protecting `try/catch`.

### Finding Description
`ValidateMultiSign.execute(byte[] rawData)` reads `words[0]`, `words[1]`, `words[2]`, and then computes a signature-array header index as `words[3].intValueSafe() / WORD_SIZE` and immediately calls `extractSigArray`/`extractBytesArray` with that offset: [1](#0-0) 

`extractBytesArray`/`extractSigArray` do check `offset > words.length - 1` for the *first* index, but then read `len = words[offset].intValueSafe()` and loop `bytesArray[i] = extractBytes(data, ...)` without validating that `offset + i + 1` stays within `words.length`, and without validating that the computed byte offsets/lengths stay within `data.length`: [2](#0-1) 

Because `words[3]` (and the derived `len`) are attacker-supplied 256-bit values reduced with `intValueSafe()`, a crafted `rawData` can make `offset`, `bytesOffset`, or `bytesLen` arbitrarily large or point past the end of `words`/`data`, causing `ArrayIndexOutOfBoundsException` (from `words[...]`) or `Arrays.copyOfRange` throwing `IndexOutOfBoundsException`/`NegativeArraySizeException` in `extractBytes`. Crucially, unlike the zk-SNARK precompiles (`VerifyMintProof`, `VerifyTransferProof`) which wrap their entire parsing/copy logic in `catch (Throwable any)` and safely return a failure result, `ValidateMultiSign.execute()` has **no enclosing try/catch around this initial parsing path** — the only `try` block in the method starts later, at the account-permission check (line 1082), after the vulnerable array construction has already completed or thrown: [3](#0-2) 

This mirrors the CVE-2020-10232 bug class (untrusted length/offset fields used directly to index/copy a fixed-size or attacker-influenced buffer without bounds checking), replacing native stack corruption with an uncaught Java runtime exception at the same trust boundary — a single contract call.

### Impact Explanation
An uncaught `RuntimeException`/`Error` thrown deep inside VM opcode execution while processing a transaction is high severity if it is not converted into a normal revert by the TVM's outer exception handling, potentially aborting block processing for all nodes that execute the same transaction (denial of service / chain halt), or at minimum causing inconsistent/failing execution for legitimate `validatemultisign` callers. I could not verify within the available index whether `Program`'s CALL/STATICCALL opcode dispatch (the code that ultimately invokes `PrecompiledContract.execute()`) wraps arbitrary `RuntimeException` from precompiles in a catch-all that safely converts it to a VM revert — my searches for the call site (`getContractForAddress`, the CALL opcode's invocation of `precompiledContract.execute`) did not return results in this index, so this could not be confirmed with certainty.

### Likelihood Explanation
`ValidateMultiSign` is a public precompile reachable by any address via a Solidity `call`/precompiled-contract invocation with fully attacker-controlled `data`; no special permission is required to trigger the code path, only a normal signed transaction that triggers a contract calling the precompile with malformed ABI-encoded input.

### Recommendation
Add bounds validation for `offset`, `bytesOffset`, and `bytesLen` against `words.length` and `data.length` inside `extractBytesArray`/`extractSigArray` before indexing, and/or wrap the entire body of `ValidateMultiSign.execute()` (like `BatchValidateSign.execute()` already does with its outer `try { return doExecute(data); } catch (Throwable t) { ... }`) so malformed input safely returns a failure result instead of propagating an exception.

### Proof of Concept
Craft `rawData` for a `validatemultisign`-style precompile call where the first three 32-byte words are set as normal ABI header words, but the word read as `words[3]` (used as the signature-array header pointer) is set to a very large value (e.g. `0xFFFFFFFF`) or a value whose `/ WORD_SIZE` offset lands beyond `words.length`. Calling this precompile from a deployed contract with such data reaches `extractSigArray`/`extractBytesArray` and triggers `ArrayIndexOutOfBoundsException` in the unguarded initial parsing path of `ValidateMultiSign.execute()` shown above.

**Caveat:** I was unable to confirm within the indexed code whether the surrounding VM/opcode dispatch (`Program`'s CALL handling of `PrecompiledContract.execute()`) already catches generic runtime exceptions and safely converts them into a transaction revert. If such a catch-all exists at that call site, this finding would be reduced to a mishandled-input/False result issue rather than a node-crashing one; I recommend verifying `actuator/src/main/java/org/tron/core/vm/program/Program.java`'s precompiled-call invocation path directly, since it could not be located via the available search tools in this session.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L399-430)
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

  private static byte[] extractBytes(byte[] data, int offset, int len) {
    return Arrays.copyOfRange(data, offset, offset + len);
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1057-1074)
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
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1080-1120)
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
    }
```
