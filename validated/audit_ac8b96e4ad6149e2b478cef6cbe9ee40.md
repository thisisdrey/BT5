### Title
Signature-count/energy mismatch in `ValidateMultiSign`/`BatchValidateSign` precompiles allows underpriced CPU-bound ECDSA verification - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
This is a structural analog of the Linea bug: a fixed/derived cost formula that does not track the actual variable-size work performed. In Linea, the L1/L2 message-claim refund was a static estimate that diverged from the real, proof-size-dependent gas cost, letting relayers either overcharge users or lose money relative to real cost. In java-tron, `ValidateMultiSign.getEnergyForData()` and `BatchValidateSign.getEnergyForData()` derive the number of signatures to be charged for purely from `data.length` (assuming a canonical, fixed-width ABI encoding), while `execute()` decodes the *actual* number of signatures from a dynamic array-length word embedded in the calldata. Before the TIP-854 guard (`allowTvmOsaka`) is activated, these two derivations can diverge, so a caller can pay energy computed for `cnt` signatures while the executor actually performs ECDSA recovery for more signatures than were charged.

### Finding Description
`ValidateMultiSign.getEnergyForData` computes: [1](#0-0) 
using only `data.length`, assuming the ABI layout is canonical (header words + fixed-width signature items). But `execute()` derives the actual signature array length from a length word read out of the calldata itself: [2](#0-1) 
and then loops over `signatures`, performing an ECDSA `recoverAddrBySign` per element: [3](#0-2) 

The same pattern exists in `BatchValidateSign`, whose `getEnergyForData` uses `(data.length / WORD_SIZE - 5) / 6` while `execute`/`doExecute` reads the actual array sizes from length words in the calldata and dispatches one ECDSA recovery task per signature via a thread pool: [4](#0-3) [5](#0-4) 

Because `bytes[]` array items in ABI encoding can be shorter than the "canonical" per-item word width assumed by the `getEnergyForData` divisor, a caller can pack more valid, differently-sized signature entries into the same total `data.length` than the fixed divisor assumes, or vice versa manipulate padding so the actual decoded array length (`sigArraySize`/`addrArraySize`, taken from an internal length word) exceeds what the energy formula charged for. Test comments confirm this exact class of issue was targeted by a TIP-854 guard (`isValidAbiEncoding`), which is only enforced when `VMConfig.allowTvmOsaka()` is active: [6](#0-5) [7](#0-6) 

Prior to (or absent) this fork activation, the divergence between the charged energy (derived from `data.length` with a fixed shape assumption) and the actual number of costly cryptographic operations performed is directly analogous to the Linea report's core defect: a static/derived fee model that does not track real variable-cost execution, letting the actor performing the underlying work (here, the full node/validator executing the transaction) do more computation than was paid for in energy.

### Impact Explanation
If the divergence can be exploited before/without the TIP-854 (`allowTvmOsaka`) guard being active on a given network, an attacker can submit transactions that trigger significantly more ECDSA signature-recovery operations (CPU-bound, non-trivial cost) than the energy fee charged accounts for. Because `BatchValidateSign` spreads work across a thread pool for up to `MAX_SIZE=16` signatures per call and this can be invoked repeatedly, an attacker paying for `cnt` signatures per the formula but triggering execution of more signatures than charged results in underpriced CPU consumption, which at scale from many crafted transactions could exhaust node CPU relative to the block energy budget — a computational resource-exhaustion / node-liveness impact (DoS), which is explicitly in scope for TVM opcodes/precompiles/energy metering.

### Likelihood Explanation
Likelihood is uncertain and moderate at best: the mismatch is only exploitable if `VMConfig.allowTvmOsaka()` (TIP-854) is not enabled, and the tests indicate the fix (`isValidAbiEncoding`) has already been implemented as a conditional guard, i.e., the maintainers are aware of and have partially/fully mitigated this bug class. Whether any deployed network currently runs with `allowTvmOsaka` disabled (making the gap exploitable) could not be confirmed from the available code index. This significantly reduces confidence that this represents a currently-exploitable issue versus an already-patched one.

### Recommendation
- Make the `isValidAbiEncoding` shape/length validation (or equivalent charge-vs-decode consistency check) unconditional in `ValidateMultiSign.execute` and `BatchValidateSign.doExecute`, rather than gated behind `VMConfig.allowTvmOsaka()`, so that the energy charged in `getEnergyForData` always upper-bounds the actual number of decoded signature recoveries performed, regardless of fork-activation state.
- Alternatively, derive the energy charge directly from the same decoded array-length words used in `execute()` (post-validation), rather than from a `data.length`-based heuristic, ensuring cost and work are always coupled 1:1 for every possible valid encoding.

### Proof of Concept
Not independently reproduced/verified — this analysis is based on static code reading of `PrecompiledContracts.java` and the referenced TIP-854 unit tests (`BatchValidateSignContractTest.testTip854RejectsMalformedCalldata`), which explicitly describe and test for "calldata whose byte length is incompatible with the (words - 5) / 6 shape the per-call energy formula already assumes." Confirming actual exploitability would require checking whether `allowTvmOsaka` is active on the target deployment and crafting a concrete calldata payload where the decoded signature/address array length exceeds the `cnt` implied by `getEnergyForData`, which was not verified in this session due to tool-call limits.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1044-1049)
```java
    @Override
    public long getEnergyForData(byte[] data) {
      long cnt = (data.length / WORD_SIZE - 5) / 5;
      // one sign 1500, half of ecrecover
      return cnt * ENGERYPERSIGN;
    }
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1051-1056)
```java
    @Override
    public Pair<Boolean, byte[]> execute(byte[] rawData) {
      if (VMConfig.allowTvmOsaka()
          && !isValidAbiEncoding(rawData, ABI_HEADER_WORDS, ABI_ITEM_WORDS)) {
        return Pair.of(false, EMPTY_BYTE_ARRAY);
      }
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1066-1078)
```java
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
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1087-1106)
```java
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
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1137-1142)
```java
    @Override
    public long getEnergyForData(byte[] data) {
      long cnt = (data.length / WORD_SIZE - 5) / 6;
      // one sign 1500, half of ecrecover
      return cnt * ENGERYPERSIGN;
    }
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1162-1200)
```java
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
      byte[] res = new byte[WORD_SIZE];
      if (isConstantCall()) {
        //for constant call not use thread pool to avoid potential effect
        for (int i = 0; i < cnt; i++) {
          if (DataWord
              .equalAddressByteArray(addresses[i], recoverAddrBySign(signatures[i], hash))) {
            res[i] = 1;
          }
        }
      } else {
        // add check
        CountDownLatch countDownLatch = new CountDownLatch(cnt);
        List<Future<RecoverAddrResult>> futures = new ArrayList<>(cnt);

        for (int i = 0; i < cnt; i++) {
          Future<RecoverAddrResult> future = workers
              .submit(new RecoverAddrTask(countDownLatch, hash, signatures[i], i));
          futures.add(future);
        }
```

**File:** framework/src/test/java/org/tron/common/runtime/vm/BatchValidateSignContractTest.java (L134-139)
```java
  // TIP-854: after activation, batchValidateSign (H=5, I=6) must reject calldata
  // whose byte length is incompatible with the (words - 5) / 6 shape the per-call
  // energy formula already assumes, returning (false, empty). The guard lives in
  // doExecute(); the outer try/catch does not mask it because the guard does not
  // throw (pure arithmetic + a static getter).
  @Test
```
