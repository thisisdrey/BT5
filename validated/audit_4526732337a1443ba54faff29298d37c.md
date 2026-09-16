### Title
DoS via unvalidated short calldata to `ValidateMultiSign` precompiled contract causes uncaught `ArrayIndexOutOfBoundsException` - ([File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java])

### Summary
The TensorFlow advisory is a CWE-20 (improper input validation) bug: `QuantizeDownAndShrinkRange` assumes `input_min`/`input_max` are scalars and indexes into them without checking shape, causing a segfault when a caller supplies a non-conforming (empty) tensor. The analogous pattern in java-tron is a TVM precompiled contract that indexes into a parsed word array derived directly from attacker-controlled calldata without first validating that the calldata contains enough words, unlike sibling precompiles in the same class which explicitly guard on `data == null || data.length != WORD_SIZE` (see e.g. `VoteCount`, `UsedVoteCount`, `TotalVoteCount`, `GetChainParameter` in the same file [1](#0-0) ).

### Finding Description
`ValidateMultiSign.execute(byte[] rawData)` parses `rawData` into `DataWord[] words = DataWord.parseArray(rawData)` and then immediately indexes `words[0]`, `words[1]`, `words[2]`, `words[3]` and (conditionally) `words[words[3].intValueSafe() / WORD_SIZE]` before entering any try/catch block: [2](#0-1) 

Unlike other precompiles in the same file that reject malformed input up front (`if (data == null || data.length != WORD_SIZE) { return ...; }`), `ValidateMultiSign` (and similarly `BatchValidateSign` prior to its `doExecute` try/catch wrapper) does not validate `rawData.length` against the number of words it is about to dereference. If a caller supplies calldata shorter than 4 words (or crafts `words[3]` to reference an out-of-range slot index), the array indexing on the parsed `DataWord[]` throws an uncaught `ArrayIndexOutOfBoundsException`, since the guard code for this class only lives inside the `try` block that wraps the *account/permission* lookup (`AccountCapsule account = this.getDeposit().getAccount(address); try { ... }`), not the earlier parsing statements [3](#0-2) .

This same bug class is explicitly documented as still reachable pre-TIP-854-activation in the project's own tests: a comment states "this precompile has no outer catch, so a too-short input raises inside the decoder; that is the documented pre-activation failure mode the TIP explicitly preserves" [4](#0-3) . The `isValidAbiEncoding` guard that would reject malformed calldata is only invoked when `VMConfig.allowTvmOsaka()` is true [5](#0-4) ; before that hard fork flag is activated on a given network, the unguarded array indexing path is live and reachable by any contract call.

### Impact Explanation
Any account (unprivileged contract deployer/caller) can trigger this by issuing a `CALL`/`STATICCALL` to the `ValidateMultiSign` precompiled contract address with calldata shorter than the minimum expected header size, or by crafting `words[3]` to point past the end of the parsed array. This throws an unhandled `RuntimeException` (`ArrayIndexOutOfBoundsException`) during TVM execution of a broadcastable smart-contract-triggering transaction. Depending on how far up the call stack this propagates before being caught by the general VM/Program exception handling, this is at minimum a per-transaction execution disruption and, per the TIP-854 test's explicit acknowledgment that "this precompile has no outer catch," represents an availability-impacting improper-input-validation defect matching CWE-20/CVSS AV:N/AC:H/S:U/A:H — consistent in class and severity with the referenced TensorFlow advisory (DoS via unchecked nonscalar/short input causing an out-of-bounds crash).

### Likelihood Explanation
High reachability: `ValidateMultiSign` is a public precompiled contract address callable from any Solidity/TVM contract via a single signed transaction — no special permission, stake, or witness/committee role is required. The only precondition is that the `allowTvmOsaka` (TIP-854) hard-fork flag is not yet active for the target chain/version, which the project's own regression test treats as an expected, currently-supported configuration state.

### Recommendation
Add an explicit length/bounds check on `rawData` (and on the derived `words` array length versus indices referenced, e.g. `words[3].intValueSafe() / WORD_SIZE`) at the very start of `ValidateMultiSign.execute` and `BatchValidateSign.doExecute`, mirroring the `data == null || data.length != WORD_SIZE`-style guards used by other precompiles in `PrecompiledContracts.java`, independent of the `allowTvmOsaka` activation flag, so malformed/short calldata is rejected with `Pair.of(false, EMPTY_BYTE_ARRAY)` rather than reaching unguarded array indexing.

### Proof of Concept
1. Deploy any contract that performs a raw `CALL` (or use `tf.raw_ops`-style direct precompile invocation analog via `eth_call`/`triggerConstantContract`) to the `ValidateMultiSign` precompiled address.
2. Supply calldata shorter than `5 * WORD_SIZE` bytes (fewer than the header words the code assumes), e.g. 32 bytes of arbitrary data.
3. On a network where `allowTvmOsaka` is not yet activated, `DataWord.parseArray(rawData)` returns an array too short to satisfy `words[3]`, causing `ArrayIndexOutOfBoundsException` to be thrown from the unguarded statements at [6](#0-5) , which is not caught within `execute` before that point, exactly as acknowledged by the existing `testTip854PreActivationNoOp` test.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1052-1120)
```java
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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1897-1922)
```java
  public static class UsedVoteCount extends PrecompiledContract {

    @Override
    public long getEnergyForData(byte[] data) {
      return 20;
    }

    @Override
    public Pair<Boolean, byte[]> execute(byte[] data) {
      if (data == null || data.length != WORD_SIZE) {
        return Pair.of(true, longTo32Bytes(0L));
      }

      byte[] address = new DataWord(data).toTronAddress();
      AccountCapsule accountCapsule = this.getDeposit().getAccount(address);

      long usedVoteCount = 0;
      if (accountCapsule != null && !accountCapsule.getVotesList().isEmpty()) {
        for (Protocol.Vote vote : accountCapsule.getVotesList()) {
          usedVoteCount += vote.getVoteCount();
        }
      }

      return Pair.of(true, longTo32Bytes(usedVoteCount));
    }
  }
```

**File:** framework/src/test/java/org/tron/common/runtime/vm/ValidateMultiSignContractTest.java (L244-260)
```java
  // TIP-854: before activation, malformed calldata reaches the legacy decoder.
  // Assert the guard is not taken — this precompile has no outer catch, so a
  // too-short input raises inside the decoder; that is the documented
  // pre-activation failure mode the TIP explicitly preserves.
  @Test
  public void testTip854PreActivationNoOp() {
    VMConfig.initAllowTvmOsaka(0);
    contract.setRepository(RepositoryImpl.createRoot(StoreFactory.getInstance()));
    try {
      Pair<Boolean, byte[]> ret = contract.execute(new byte[(5 + 1) * 32]);
      // If the decoder happened to handle it without raising, we must not have
      // taken the post-activation reject path (false, empty).
      Assert.assertNotSame(ByteUtil.EMPTY_BYTE_ARRAY, ret.getRight());
    } catch (RuntimeException expectedLegacyBehaviour) {
      // Pre-activation: decoder may throw — this is the existing behaviour.
    }
  }
```
