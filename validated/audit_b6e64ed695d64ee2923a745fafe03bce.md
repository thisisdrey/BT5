### Title
Unvalidated attacker-controlled array-length in `ValidateMultiSign` precompile causes uncaught allocation/index exception - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
The `ValidateMultiSign` precompiled contract extracts a variable-length signature array from calldata using a length field that is fully attacker-controlled, but the length is only bounds-checked *after* the array has already been allocated and indexed — and only when a not-yet-active hard-fork flag is set. This mirrors the CVE-2021-29578 root cause: a size argument taken from user input is used to allocate/iterate a buffer without first validating it against the true size of the backing data.

### Finding Description
`ValidateMultiSign.execute()` reads the signature-array offset/length straight out of attacker-supplied `rawData` and passes it to `extractSigArray`/`extractBytesArray` *before* entering the guarded `try` block that catches `Throwable`: [1](#0-0) 

The extraction helpers allocate an array sized directly from the decoded `DataWord`, with no validation that the length is non-negative or consistent with the actual `words`/`data` array size: [2](#0-1) [3](#0-2) 

The only length sanity check (`sigArraySize > MAX_SIZE`) is itself gated behind `VMConfig.allowTvmSelfdestructRestriction()`, and the stronger structural check (`isValidAbiEncoding`) is gated behind `VMConfig.allowTvmOsaka()`: [4](#0-3) 

If either flag is not yet active on the network, a caller can supply a crafted length word (e.g. one that decodes to a negative `int` or one far larger than the actual calldata) that is used directly as `new byte[len][]` and as a loop bound indexing into `words`/`data`. This can throw `NegativeArraySizeException`, `ArrayIndexOutOfBoundsException`, or trigger a huge allocation (`OutOfMemoryError`) — all **outside** the `try { ... } catch (Throwable t)` block that wraps only the signature-verification logic further down: [5](#0-4) 

Unlike `BatchValidateSign`, whose `execute()` wraps the *entire* `doExecute()` (including the extraction calls) in `try { ... } catch (Throwable t)`: [6](#0-5) 
`ValidateMultiSign` has no equivalent outer guard, so an exception thrown during extraction propagates out of `contract.execute(data)` and up through `Program.callToPrecompiledAddress`, which does not catch exceptions around the `contract.execute(data)` call: [7](#0-6) 

I found evidence that the project is aware of this bug class — TIP-854 hardening and tests (`testTip854RejectsMalformedCalldata`, `testTip854OuterFrameContainment`) explicitly validate that malformed calldata is rejected and exceptions are contained — but that hardening for `ValidateMultiSign`'s extraction path is conditioned on `VMConfig.allowTvmOsaka()`, a feature flag that is not confirmed active by default: [8](#0-7) [9](#0-8) 

### Impact Explanation
An unauthenticated contract deployer/caller can invoke the `ValidateMultiSign` precompile (reachable from any smart contract via a `CALL`/`STATICCALL` to its fixed precompile address) with crafted calldata whose length word is negative or grossly oversized relative to the real data. This throws an unhandled exception inside `Program.callToPrecompiledAddress`, which has no surrounding exception guard for the `contract.execute(data)` call. Depending on how far up the call stack the exception propagates before being caught (actuator-level, block-application level, or unguarded), this can range from an unexpectedly-failed transaction to an uncaught exception during block application in `Manager`, which would halt/crash node transaction processing for that block — a chain-halting or node-crash condition reachable from a single crafted contract call.

### Likelihood Explanation
High reachability: the precompile is invocable by anyone who can deploy or call a smart contract that issues a `CALL` to the `ValidateMultiSign` address with arbitrary calldata — no special privileges required. The vulnerable code path (extraction before the exception-catching `try` block) is exercised whenever `VMConfig.allowTvmSelfdestructRestriction()`/`allowTvmOsaka()` guards are not both active, which is a plausible historical/default network state given these are named as forward-looking TIP-854/Osaka hardening flags.

### Recommendation
Move the calldata-shape validation (equivalent to `isValidAbiEncoding` and the `sigArraySize > MAX_SIZE` check) out of feature-flag gating and make it unconditional in `ValidateMultiSign.execute()`, performed before any array extraction. Additionally, wrap the entire `execute()` body of `ValidateMultiSign` (including the `extractSigArray`/`extractBytesArray` calls) in the same `try { ... } catch (Throwable t) { return Pair.of(true, DATA_FALSE); }` pattern used by `BatchValidateSign`, so malformed input can never throw past the precompile boundary. Also add explicit bounds checks in `extractBytes32Array` (currently has none) mirroring the `offset > words.length - 1` guard already present in `extractBytesArray`/`extractSigArray`.

### Proof of Concept
1. Deploy a trivial contract that performs `CALL` to the `ValidateMultiSign` precompile address (`0x0000...1010` or equivalent) with calldata: 5 header words followed by a "length" word at the signature-offset slot whose big-endian value, when passed through `DataWord.intValueSafe()`, yields a negative or extremely large `int` (e.g., a `DataWord` set to `0xFFFFFFFF...` such that only the low 4 bytes are non-zero and encode `-1` or a value like `0x7FFFFFFF`).
2. Ensure the network has not activated `allowTvmSelfdestructRestriction`/`allowTvmOsaka` (or invoke the contract logic directly against `PrecompiledContracts.ValidateMultiSign` for a unit-level repro, as done in `ValidateMultiSignContractTest`).
3. Observe `extractBytesArray`/`extractSigArray` throwing `NegativeArraySizeException`/`OutOfMemoryError`/`ArrayIndexOutOfBoundsException` from `words[offset].intValueSafe()`-derived length, outside `ValidateMultiSign`'s inner `try/catch`, propagating uncaught through `contract.execute(data)` in `Program.callToPrecompiledAddress` (no surrounding catch at lines 1752–1766).

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1052-1057)
```java
    public Pair<Boolean, byte[]> execute(byte[] rawData) {
      if (VMConfig.allowTvmOsaka()
          && !isValidAbiEncoding(rawData, ABI_HEADER_WORDS, ABI_ITEM_WORDS)) {
        return Pair.of(false, EMPTY_BYTE_ARRAY);
      }
      DataWord[] words = DataWord.parseArray(rawData);
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

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L1752-1766)
```java
      Pair<Boolean, byte[]> out = contract.execute(data);

      if (out.getLeft()) { // success
        this.refundEnergy(msg.getEnergy().longValue() - requiredEnergy, CALL_PRE_COMPILED);
        this.stackPushOne();
        returnDataBuffer = out.getRight();
        deposit.commit();
      } else {
        // spend all energy on failure, push zero and revert state changes
        this.refundEnergy(0, CALL_PRE_COMPILED);
        this.stackPushZero();
        if (Objects.nonNull(this.result.getException())) {
          throw result.getException();
        }
      }
```

**File:** framework/src/test/java/org/tron/common/runtime/vm/OperationsTest.java (L861-902)
```java
  // TIP-854 outer-frame containment: a CALL to validateMultiSign or
  // batchValidateSign with malformed calldata must (a) push 0 onto the outer
  // stack, (b) leave the outer frame free of any propagated exception, and
  // (c) allow the outer frame to continue executing afterwards.
  @Test
  public void testTip854OuterFrameContainment() throws ContractValidateException {
    byte prePrefixByte = DecodeUtil.addressPreFixByte;
    DecodeUtil.addressPreFixByte = Constant.ADD_PRE_FIX_BYTE_MAINNET;
    VMConfig.initAllowTvmOsaka(1);
    try {
      for (PrecompiledContracts.PrecompiledContract contract :
          new PrecompiledContracts.PrecompiledContract[]{
              new PrecompiledContracts.ValidateMultiSign(),
              new PrecompiledContracts.BatchValidateSign()}) {
        invoke = new ProgramInvokeMockImpl();
        InternalTransaction interTrx = new InternalTransaction(
            Protocol.Transaction.getDefaultInstance(),
            InternalTransaction.TrxType.TRX_UNKNOWN_TYPE);
        program = new Program(new byte[0], new byte[0], invoke, interTrx);
        // inDataSize=0 ⇒ data=[] ⇒ fewer than H=5 head words ⇒ guard rejects.
        MessageCall messageCall = new MessageCall(
            Op.CALL, new DataWord(10000),
            DataWord.ZERO(), DataWord.ZERO(),
            DataWord.ZERO(), DataWord.ZERO(),
            DataWord.ZERO(), DataWord.ZERO(),
            DataWord.ZERO(), false);
        program.callToPrecompiledAddress(messageCall, contract);

        Assert.assertNull(contract.getClass().getSimpleName()
                + ": outer frame must not inherit an exception",
            program.getResult().getException());
        Assert.assertEquals(contract.getClass().getSimpleName() + ": inner CALL pushes 0",
            DataWord.ZERO(), program.getStack().pop());
        // Outer frame continues: another stack op works without throwing.
        program.stackPush(new DataWord(1));
        Assert.assertEquals(new DataWord(1), program.getStack().pop());
      }
    } finally {
      VMConfig.initAllowTvmOsaka(0);
      DecodeUtil.addressPreFixByte = prePrefixByte;
    }
  }
```

**File:** framework/src/test/java/org/tron/common/runtime/vm/ValidateMultiSignContractTest.java (L158-192)
```java
  // TIP-854: after activation, validateMultiSign (H=5, I=5) must reject calldata
  // whose byte length is incompatible with the (words - 5) / 5 shape the per-call
  // energy formula already assumes, returning (false, empty).
  @Test
  public void testTip854RejectsMalformedCalldata() {
    VMConfig.initAllowTvmOsaka(1);
    try {
      // Bucket 1: 32-aligned head + sub-word trailing bytes (r=1, r=31).
      for (int r : new int[]{1, 31}) {
        byte[] data = new byte[(5 + 5) * 32 + r];
        Pair<Boolean, byte[]> ret = contract.execute(data);
        Assert.assertFalse("non-32-aligned len=" + data.length, ret.getLeft());
        Assert.assertSame(ByteUtil.EMPTY_BYTE_ARRAY, ret.getRight());
      }
      // Bucket 2: fewer than the static head's 5 words.
      for (int bytes : new int[]{0, 32, 64, 96, 128}) {
        Pair<Boolean, byte[]> ret = contract.execute(new byte[bytes]);
        Assert.assertFalse("len=" + bytes + " < 5 words", ret.getLeft());
        Assert.assertSame(ByteUtil.EMPTY_BYTE_ARRAY, ret.getRight());
      }
      // Bucket 3: 32-aligned but tail not a multiple of I=5 words (k = 1..4).
      for (int k = 1; k <= 4; k++) {
        byte[] data = new byte[(5 + k) * 32];
        Pair<Boolean, byte[]> ret = contract.execute(data);
        Assert.assertFalse("aligned bad-tail k=" + k, ret.getLeft());
        Assert.assertSame(ByteUtil.EMPTY_BYTE_ARRAY, ret.getRight());
      }
      // Null calldata: explicit spec clause.
      Pair<Boolean, byte[]> ret = contract.execute(null);
      Assert.assertFalse("null calldata", ret.getLeft());
      Assert.assertSame(ByteUtil.EMPTY_BYTE_ARRAY, ret.getRight());
    } finally {
      VMConfig.initAllowTvmOsaka(0);
    }
  }
```
