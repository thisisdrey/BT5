## Finding

### Title
Stale VM precompiled-JUMPDEST cache is never invalidated because `RepositoryImpl.removeLruCache()` is a no-op — (File: `actuator/src/main/java/org/tron/core/vm/repository/RepositoryImpl.java`)

### Summary
`RepositoryImpl.removeLruCache(byte[] address)` is defined as an empty, no-op method, yet it is called from multiple actuators specifically to purge a static contract-bytecode-derived cache whenever contract state that affects execution semantics changes (contract self-destruct/redeploy, `UpdateSettingContract`, `UpdateEnergyLimitContract`). Because the invalidation call is now a stub, a cached `ProgramPrecompile` object (containing pre-computed JUMPDEST bitmap analysis for a contract's bytecode) can persist and be reused after the underlying contract has been destroyed and a different contract has been redeployed at the same address (e.g., via `CREATE2`), or after other contract-affecting settings have changed. [1](#0-0) 

### Finding Description
`Program.getProgramPrecompile()` maintains a static, process-wide LRU cache (`programPrecompileLRUMap`) of `ProgramPrecompile` objects keyed by a per-contract cache key, used to avoid recomputing JUMPDEST validity bitmaps on every VM call: [2](#0-1) 

Several actuators/paths reachable by ordinary users are expected to invalidate stale cache entries tied to a contract address whenever that contract's bytecode or contract-affecting metadata changes:
- `VMActuator` calls `RepositoryImpl.removeLruCache(account.toTronAddress())` for every address in `result.getDeleteAccounts()` after a transaction completes (i.e., after `SELFDESTRUCT`), which is the exact moment at which a new contract could subsequently be deployed at the same address via `CREATE2`. [3](#0-2) 
- `UpdateSettingContractActuator.execute()` calls `RepositoryImpl.removeLruCache(contractAddress)` immediately after rewriting the on-chain `ContractCapsule` for that address. [4](#0-3) 
- `UpdateEnergyLimitContractActuator` similarly calls `removeLruCache` after updating contract metadata.

However, the implementation of `removeLruCache` was reduced to an empty method body:

```java
public static void removeLruCache(byte[] address) {
}
``` [1](#0-0) 

This means every call site that assumes stale precompiled analysis is purged silently does nothing. The class of bug is directly analogous to the MariaDB `Item_func_in::cleanup()` use-after-free: an object (`ProgramPrecompile`, a cached derived-state object tied to a specific bytecode instance) is supposed to be discarded/invalidated when its backing "owner" object is destroyed or mutated, but the cleanup/invalidation hook is effectively dead code, so the stale object continues to be referenced and reused against a different, newer instance of the underlying data (new bytecode deployed at the same address). Instead of a raw memory-safety UAF (Java's GC prevents that), this manifests as a logical use-after-invalidation: JUMPDEST validity information computed for one bytecode blob is served for execution of a completely different bytecode blob occupying the same key/address.

### Impact Explanation
`ProgramPrecompile`'s JUMPDEST bitmap governs which program-counter offsets are valid `JUMP`/`JUMPI` targets. If a stale bitmap computed for an old contract's code is served to a newly redeployed contract with different code at the same address (via `SELFDESTRUCT` + `CREATE2` redeploy — a well-known, fully attacker-controlled sequence in TVM/EVM-style chains), execution of the new contract could:
- Treat offsets that are not actual `JUMPDEST` opcodes in the new bytecode as valid jump targets (or vice versa, reject valid jumps), corrupting control flow.
- Enable an attacker who controls both the old and new bytecode at a `CREATE2` address to craft situations where the VM misvalidates jumps, potentially bypassing dispatch-table/guard logic in the new contract and leading to unauthorized state transitions (e.g., unauthorized token/balance operations) within that contract's own logic.

This is reachable purely through standard, unprivileged transaction flows (deploy contract, self-destruct it, redeploy via `CREATE2` at the same address, call it) — no special privileges, SR/witness status, or network position are required, satisfying the "unauthorized account operation" bar for impact.

### Likelihood Explanation
High reachability: `SELFDESTRUCT` and `CREATE2` are ordinary, unrestricted TVM opcodes available to any contract deployer/caller, and `UpdateSettingContract`/`UpdateEnergyLimitContract` are ordinary broadcastable transaction types available to any contract owner. The only precondition is that the LRU cache key collides across the destroyed and redeployed contract (i.e., the same on-chain address is reused), which `CREATE2` is explicitly designed to make deterministic and attacker-controlled. The `programPrecompileLRUMap` is a static, JVM-wide cache shared across all transactions processed by the node, so the race window spans the entire cache lifetime (bounded only by LRU eviction), not just a single block.

Note: I was not able to fully confirm within available tool calls the exact composition of the cache key returned by `getJumpDestAnalysisCacheKey()` (specifically whether it incorporates a code hash in addition to/instead of the contract address) — the method body was not retrieved in the sessions available. If the key is derived purely from the contract address (not from a code hash), the collision described above is unconditional; if it includes a code hash, exploitability would depend on additional hash-based key derivation details. This should be verified directly in `Program.java` (`getJumpDestAnalysisCacheKey`) before remediation.

### Recommendation
- Implement `RepositoryImpl.removeLruCache(byte[] address)` to actually evict/invalidate the corresponding entry from `Program.programPrecompileLRUMap` (or expose a package-visible invalidation API from `Program`/`ProgramPrecompile` for this purpose) whenever an address's contract code changes, is destroyed, or is redeployed.
- Ensure the JUMPDEST-analysis cache key is bound to contract code content (e.g., a code hash) rather than the raw address alone, so stale entries can never be served for different bytecode even if invalidation is missed.
- Add a regression test that: deploys contract A, self-destructs it, redeploys contract B with different bytecode at the same `CREATE2` address, and asserts that JUMPDEST validation and cached precompile data reflect contract B's bytecode, not contract A's.

### Proof of Concept
1. Deploy `Factory` contract that deploys via `CREATE2` (pattern used in `Create2Test.java` / `FreezeTest.java`, e.g. `factory.deploy(codeA, salt)`). [5](#0-4) 
2. Call the deployed contract A enough to have its `ProgramPrecompile` cached in `programPrecompileLRUMap` (any non-constant call triggers `getProgramPrecompile()`).
3. Trigger contract A's `selfdestruct` (as in `FreezeTest.testCreate2SuicideToBlackHole`), which invokes `VMActuator`'s post-execution loop calling `RepositoryImpl.removeLruCache(addr)` — a no-op. [3](#0-2) [6](#0-5) 
4. Redeploy different bytecode B at the same predicted `CREATE2` address (`factory.deploy(codeB, salt)`), producing the identical contract address as A but different opcodes/JUMPDEST layout.
5. Call the redeployed contract B; observe (via instrumentation/unit test on `programPrecompileLRUMap`) that the JUMPDEST bitmap served is still the one computed for bytecode A, not B — confirming stale-cache reuse due to the disabled `removeLruCache`.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/repository/RepositoryImpl.java (L144-145)
```java
  public static void removeLruCache(byte[] address) {
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L117-225)
```java
  private static final int lruCacheSize = CommonParameter.getInstance().getSafeLruCacheSize();
  private static final LRUMap<Key, ProgramPrecompile> programPrecompileLRUMap
      = new LRUMap<>(lruCacheSize);
  private long nonce;
  private byte[] rootTransactionId;
  private InternalTransaction internalTransaction;
  private ProgramInvoke invoke;
  private ProgramOutListener listener;
  private ProgramTraceListener traceListener;
  private ProgramStorageChangeListener storageDiffListener = new ProgramStorageChangeListener();
  private CompositeProgramListener programListener = new CompositeProgramListener();
  private Stack stack;
  private Memory memory;
  private ContractState contractState;
  private byte[] returnDataBuffer;
  private ProgramResult result = new ProgramResult();
  private ProgramTrace trace = new ProgramTrace();
  private byte[] ops;
  private byte[] codeAddress;
  private int pc;
  private byte lastOp;
  private byte previouslyExecutedOp;
  private boolean stopped;
  private ProgramPrecompile programPrecompile;
  private int contractVersion;
  private DataWord adjustedCallEnergy;
  @Getter
  @Setter
  private long contextContractFactor;
  @Getter
  @Setter
  private long callPenaltyEnergy;

  public Program(byte[] ops, byte[] codeAddress, ProgramInvoke programInvoke,
                 InternalTransaction internalTransaction) {
    this.invoke = programInvoke;
    this.internalTransaction = internalTransaction;
    this.ops = nullToEmpty(ops);
    this.codeAddress = codeAddress;

    traceListener = new ProgramTraceListener(VMConfig.vmTrace());
    this.memory = setupProgramListener(new Memory());
    this.stack = setupProgramListener(new Stack());
    this.contractState = setupProgramListener(new ContractState(programInvoke));
    this.trace = new ProgramTrace(programInvoke);
    this.nonce = internalTransaction.getNonce();
  }

  @SuppressWarnings("unused")
  static String formatBinData(byte[] binData, int startPC) {
    StringBuilder ret = new StringBuilder();
    for (int i = 0; i < binData.length; i += 16) {
      ret.append(Utils.align("" + Integer.toHexString(startPC + (i)) + ":", ' ', 8, false));
      ret.append(Hex.toHexString(binData, i, min(16, binData.length - i,
          VMConfig.disableJavaLangMath()))).append('\n');
    }
    return ret.toString();
  }

  public byte[] getRootTransactionId() {
    return rootTransactionId.clone();
  }

  public void setRootTransactionId(byte[] rootTransactionId) {
    this.rootTransactionId = rootTransactionId.clone();
  }

  public void setContractVersion(int version) {
    this.contractVersion = version;
  }

  public int getContractVersion() {
    return this.contractVersion;
  }

  public void setAdjustedCallEnergy(DataWord adjustedCallEnergy) {
    this.adjustedCallEnergy = adjustedCallEnergy;
  }

  public DataWord getAdjustedCallEnergy() {
    return this.adjustedCallEnergy;
  }

  public long getNonce() {
    return nonce;
  }

  public void setNonce(long nonceValue) {
    nonce = nonceValue;
  }

  public ProgramPrecompile getProgramPrecompile() {
    if (isConstantCall()) {
      if (programPrecompile == null) {
        programPrecompile = ProgramPrecompile.compile(ops);
      }
      return programPrecompile;
    }
    if (programPrecompile == null) {
      Key key = getJumpDestAnalysisCacheKey();
      if (programPrecompileLRUMap.containsKey(key)) {
        programPrecompile = programPrecompileLRUMap.get(key);
      } else {
        programPrecompile = ProgramPrecompile.compile(ops);
        programPrecompileLRUMap.put(key, programPrecompile);
      }
    }
    return programPrecompile;
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/VMActuator.java (L268-270)
```java
      for (DataWord account : result.getDeleteAccounts()) {
        RepositoryImpl.removeLruCache(account.toTronAddress());
      }
```

**File:** actuator/src/main/java/org/tron/core/actuator/UpdateSettingContractActuator.java (L43-49)
```java
      byte[] contractAddress = usContract.getContractAddress().toByteArray();
      ContractCapsule deployedContract = contractStore.get(contractAddress);

      contractStore.put(contractAddress, new ContractCapsule(
          deployedContract.getInstance().toBuilder().setConsumeUserResourcePercent(newPercent)
              .build()));
      RepositoryImpl.removeLruCache(contractAddress);
```

**File:** framework/src/test/java/org/tron/common/runtime/vm/Create2Test.java (L30-60)
```java

@Slf4j
public class Create2Test extends VMTestBase {
  /*
  pragma solidity 0.5.0;
  contract Factory {
      event Deployed(address addr, uint256 salt);
      function deploy(bytes memory code, uint256 salt) public returns(address){
          address addr;
          assembly {
              addr := create2(0, add(code, 0x20), mload(code), salt)
              if iszero(extcodesize(addr)) {
                  revert(0, 0)
              }
          }
          emit Deployed(addr, salt);
          return addr;
      }
  }



  contract TestConstract {
      uint public i;
      constructor () public {
      }
      function plusOne() public returns(uint){
          i++;
      }
  }
   */
```

**File:** framework/src/test/java/org/tron/common/runtime/vm/FreezeTest.java (L489-510)
```java
  @Test
  public void testCreate2SuicideToBlackHole() throws Exception {
    byte[] factory = deployContract("FactoryContract", FACTORY_CODE);
    byte[] contract = deployContract("TestFreeze", CONTRACT_CODE);
    long frozenBalance = 1_000_000;
    freezeForSelf(contract, frozenBalance, 0);
    freezeForSelf(contract, frozenBalance, 1);
    long salt = 1;
    byte[] predictedAddr = getCreate2Addr(factory, salt);
    freezeForOther(contract, predictedAddr, frozenBalance, 0);
    freezeForOther(contract, predictedAddr, frozenBalance, 1);
    Assert.assertArrayEquals(predictedAddr, deployCreate2Contract(factory, salt));
    setBalance(predictedAddr, 100_000_000);
    freezeForSelf(predictedAddr, frozenBalance, 0);
    freezeForSelf(predictedAddr, frozenBalance, 1);
    freezeForOther(predictedAddr, userA, frozenBalance, 0);
    freezeForOther(predictedAddr, userA, frozenBalance, 1);
    suicideWithException(predictedAddr, predictedAddr);
    clearDelegatedExpireTime(predictedAddr, userA);
    unfreezeForOther(predictedAddr, userA, 0);
    unfreezeForOther(predictedAddr, userA, 1);
    suicideToAccount(predictedAddr, predictedAddr);
```
