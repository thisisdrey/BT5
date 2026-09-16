### Title
Out-of-bounds/uncontrolled array access in `ValidateMultiSign`/`BatchValidateSign` precompile byte-array extraction can escape TVM containment before TIP-854 activation - (`actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
The `extractBytesArray` / `extractSigArray` helpers used by the `ValidateMultiSign` and `BatchValidateSign` TVM precompiled contracts compute a byte offset and length directly from attacker-controlled call-data words and slice the raw input buffer with those unchecked values, exactly mirroring the ksmbd CVE-2023-38428 root cause of trusting an offset/length pair taken from the buffer itself without validating it against the buffer's real bounds.

### Finding Description
`extractBytesArray` reads `len`, `bytesOffset`, and `bytesLen` straight out of the caller-supplied `words` array and then calls `extractBytes`, which does an unchecked `Arrays.copyOfRange(data, offset, offset + len)`: [1](#0-0) 

`words` itself is parsed from the raw precompile input (`rawData`/`data`) supplied by any contract that performs a `CALL`/`STATICCALL` to the `ValidateMultiSign` (`0x...1006`-style) or `BatchValidateSign` precompile addresses: [2](#0-1) [3](#0-2) 

Just like ksmbd's `smb2pdu.c` computing the UserName location from an attacker-controlled security-buffer offset without checking it against the packet's real bounds, `bytesOffset`/`bytesLen` here are computed from attacker-controlled words and used to slice `rawData`/`data` without any check that `(bytesOffset + offset + 2) * WORD_SIZE + bytesLen` stays within the actual buffer length. `words[offset + i + 1]`/`words[offset + bytesOffset + 1]` accesses can also run past the end of the `words` array (`ArrayIndexOutOfBoundsException`), and a crafted negative/huge `bytesLen` can trigger `NegativeArraySizeException` in `Arrays.copyOfRange`.

Crucially, `ValidateMultiSign.execute` only wraps the *post-extraction* account/signature-recovery logic in `try { } catch (Throwable t)`; the calls to `extractBytesArray`/`extractSigArray` themselves happen **before** that try block and are unguarded: [4](#0-3) 

An ABI-encoding sanity check (`isValidAbiEncoding`) was added to mitigate this, but it is gated behind the `VMConfig.allowTvmOsaka()` feature flag: [5](#0-4) [6](#0-5) 

The presence of a dedicated regression test named for "TIP-854 outer-frame containment" confirms this was a recognized, real issue: a malformed-calldata CALL to `validateMultiSign`/`batchValidateSign` could previously let an exception escape the precompile and disrupt the *outer* calling frame instead of being confined to the inner CALL (which should just push `0`): [7](#0-6) 

`Program.callToPrecompiledAddress` invokes `contract.execute(data)` with no surrounding try/catch of its own besides what the precompile provides internally, so any exception thrown out of `extractBytesArray` before `allowTvmOsaka` is active propagates up through this call site: [8](#0-7) 

### Impact Explanation
On any network/hard-fork state where `allowTvmOsaka` has not yet been activated (i.e., prior to that TVM upgrade), any unprivileged smart contract can `CALL` the `ValidateMultiSign` or `BatchValidateSign` precompile with a crafted offset/length word to trigger an uncaught `ArrayIndexOutOfBoundsException` or `NegativeArraySizeException` inside `extractBytesArray`. Because this happens before the internal `try/catch(Throwable)` block in `ValidateMultiSign.execute`, the exception is not confined to the inner CALL frame — it propagates out of the precompile call, contrary to normal TVM containment semantics for external calls. Depending on how far up the call stack it propagates, this can corrupt transaction execution accounting, cause a transaction/block-processing halt on a node executing this call as part of block application, or produce a deterministic-availability issue (denial-of-service to that call path) for any node type that has not yet enabled the Osaka TVM feature.

### Likelihood Explanation
Any address can deploy a trivial contract that issues a `CALL`/`STATICCALL` to the precompile with an invalid word layout — this requires no special privilege, stake, or witness role, only broadcasting one transaction. The bug is directly and deterministically reachable through public TVM opcode/precompile call semantics, matching the "unprivileged contract deployer / caller" threat model in scope.

### Recommendation
- Make the `isValidAbiEncoding` bounds check (or an equivalent explicit bounds validation of `bytesOffset`, `bytesLen`, and index accesses into `words`) unconditional in `extractBytesArray`, `extractSigArray`, and `extractBytes32Array`, rather than gating it behind `VMConfig.allowTvmOsaka()`.
- Wrap the full body of `ValidateMultiSign.execute` and `BatchValidateSign.doExecute` (including the extraction calls) in the existing `try/catch(Throwable)` so any parsing failure safely returns `Pair.of(true, DATA_FALSE)`/`false` instead of allowing an exception to escape the precompile boundary.
- Add negative/overflow guards on `bytesOffset`/`bytesLen` and range checks on `offset + i + 1`/`offset + bytesOffset + 1` against `words.length` before array access.

### Proof of Concept
1. Deploy a contract that performs a raw `CALL` (via inline assembly or a low-level call) to the `ValidateMultiSign` precompile address with call-data crafted so that word[3] (the array-offset word) resolves to an index whose subsequent "length" word is a large or negative number, or whose element offset points past the end of `rawData`.
2. On a node where `allowTvmOsaka` is not active, invoke this contract via a normal transaction.
3. Observe that `extractBytesArray`/`extractSigArray` throws `ArrayIndexOutOfBoundsException`/`NegativeArraySizeException` before the internal `try/catch` in `ValidateMultiSign.execute`, and that this exception is not translated into a normal "push 0" outer-call failure the way `OperationsTest.testTip854OuterFrameContainment` verifies for the patched (Osaka-enabled) path — confirm by comparing behavior with `VMConfig.initAllowTvmOsaka(1)` vs `0`, per the referenced test at [9](#0-8) .

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1051-1082)
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
      if (account != null) {
        try {
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1156-1177)
```java
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
```

**File:** framework/src/test/java/org/tron/common/runtime/vm/OperationsTest.java (L861-901)
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
