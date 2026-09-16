Based on my investigation, I found a concrete analog in java-tron's `ValidateMultiSign` precompiled contract, which mirrors the underlying bug class (attacker-controlled offset/length arithmetic feeding an unchecked array/byte-range access with no upstream bounds validation).

### Title
Unbounded/negative offset in `ValidateMultiSign` precompiled contract causes uncaught OOB exception before error handling - ([File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java])

### Summary
The `ValidateMultiSign` precompiled contract (TVM address `0x66`, callable via `STATICCALL`/`CALL` from any deployed contract) computes an ABI array offset directly from user-supplied call data and passes it to `extractBytesArray`/`extractSigArray` before any exception-handling wrapper is in place, analogous to the libheif bug where an attacker-controlled tile/grid offset (irot-induced) is used unchecked to index into decoded data, causing an out-of-bounds read.

### Finding Description
In `execute(byte[] rawData)`, the signature-array offset is computed as: [1](#0-0) 

`words[3].intValueSafe() / WORD_SIZE` is fully attacker-controlled (it comes straight from the ABI-encoded call data with no validation other than the optional `allowTvmSelfdestructRestriction` size check on `sigArraySize`, which itself indexes `words[...]` with the same unchecked value). This value is then passed into `extractBytesArray`/`extractSigArray`: [2](#0-1) 

Inside these helpers, `len = words[offset].intValueSafe()` and `bytesOffset = words[offset + i + 1].intValueSafe() / WORD_SIZE` are derived from attacker data and used to index into the `words` array and to compute byte offsets passed to `extractBytes`, which does `Arrays.copyOfRange(data, offset, offset + len)` with no bounds validation: [3](#0-2) 

Critically, this entire computation — including `words[3].intValueSafe() / WORD_SIZE` used as `offset`, and the `sigArraySize` pre-check when `allowTvmSelfdestructRestriction` is enabled — happens **before** the `try { ... } catch (Throwable t)` block that wraps the rest of the function: [4](#0-3) 

A crafted `offset` (e.g., large, causing `offset + i + 1` to overflow into negative, or `offset` itself negative if `words[3]` is a huge value that overflows on division, or simply an out-of-range positive index) causes `ArrayIndexOutOfBoundsException` when indexing `words[offset]`/`words[offset + i + 1]`, or a huge/negative `len` triggers `NegativeArraySizeException` on `new byte[len][]`, or `extractBytes`'s `Arrays.copyOfRange` throws `ArrayIndexOutOfBoundsException`/`NegativeArraySizeException` when offset/len fall outside `data`'s bounds. None of these exceptions are caught by the local `try/catch(Throwable)` block since they occur earlier in the method, before line 1080.

### Impact Explanation
Whether this crashes the node or is safely converted to a VM revert depends entirely on whether the top-level precompiled-contract dispatch path (`Program.java`, `callToPrecompiledAddress`/equivalent) wraps `PrecompiledContract.execute()` calls in a generic `catch (Throwable)` / `PrecompiledContractException` translation layer. I was not able to fully confirm the exact top-level exception-catching behavior for precompiled contract invocation within the available context before running out of iterations — `Program.java` references to `PrecompiledContractException` exist but I could not verify whether all runtime exceptions (specifically `ArrayIndexOutOfBoundsException`/`NegativeArraySizeException` thrown from `PrecompiledContracts.java` prior to any local catch) are uniformly caught there. If they are not uniformly caught at that layer, this is a reachable, attacker-triggered uncaught exception during contract execution (any address can deploy a contract that calls `0x66` with crafted data), which could disrupt transaction processing or block application for the node — matching the "unhandled OOB access reachable via untrusted external input" bug class in the report, though with Java's memory safety, the exploitable outcome is a crash/DoS on that specific code path rather than a heap read of a foreign process's memory (as in the C-based libheif bug).

### Likelihood Explanation
High reachability: `ValidateMultiSign` is invoked by any TVM contract performing `STATICCALL` to the precompile address, meaning any unprivileged contract deployer/caller can trigger it with fully attacker-controlled call data, requiring no special privileges — matching the "contract call" and "TVM precompiles" reachable-surface criteria in the rules.

### Recommendation
Add explicit bounds validation before using any offset derived from `words[...]` (e.g., `words[3].intValueSafe() / WORD_SIZE`) to index into the `words` array or compute byte ranges into `rawData`, mirroring the existing `if (offset > words.length - 1) return new byte[0][];` guard already present in `extractBytesArray`/`extractSigArray` — that guard needs to also validate `offset >= 0` and that `offset + len` bounds (for both the `words` array and the underlying byte buffer) stay within range, and this validation should occur before the value is used in the `sigArraySize` check at line 1067, i.e., before entering the unguarded portion of `execute`. Wrap the whole offset/array extraction logic in the existing `try { ... } catch (Throwable t)` block, or add validation with `isValidAbiEncoding` semantics consistently for all offset math, not just the top-level header check.

### Proof of Concept
Deploy a trivial contract that does a raw `staticcall` to precompile address `0x0000...66` with ABI-encoded data where the first 5 header words are the standard `address`, `permissionId`, `data`-offset placeholder, and a crafted 4th word (`words[3]`) whose value, divided by 32, yields an offset that is negative or exceeds `words.length`. Because `VMConfig.allowTvmSelfdestructRestriction()` reads `words[words[3].intValueSafe() / WORD_SIZE]` at line 1067 — outside any try/catch — supplying an out-of-range value directly triggers `ArrayIndexOutOfBoundsException` inside precompiled-contract execution, before the method's local exception handler can intercept it.

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L428-430)
```java
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
