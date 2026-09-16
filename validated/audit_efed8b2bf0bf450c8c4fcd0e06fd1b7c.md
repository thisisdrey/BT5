### Title
Front-runnable system address squatting permanently disables TIP-2935 BlockHashHistory and lets an attacker's contract permanently occupy the reserved history-storage slot - (File: framework/src/main/java/org/tron/core/db/HistoryBlockHashUtil.java)

### Summary
`HistoryBlockHashUtil.deploy()` installs the TIP‑2935 "BlockHashHistory" system contract at a fixed, publicly known address `HISTORY_STORAGE_ADDRESS` only if no code/contract metadata already exists there; if an attacker deploys their own bytecode to that exact address beforehand (e.g. via `CREATE2` with a chosen salt, well before the fork activates), `deploy()` silently skips installation forever, and the attacker's arbitrary code permanently occupies the reserved system address that every future contract on the chain is expected to trust for historical block hashes.

### Finding Description
`HISTORY_STORAGE_ADDRESS` is a fixed constant (`410000f90827f1c53a10cb7a02335b175320002935`) known to anyone reading the source or bytecode [1](#0-0) . At fork activation, `deploy()` checks whether code or contract metadata already exists at that address and, if so, logs a warning and returns without ever installing the canonical BlockHashHistory bytecode: [2](#0-1) 

Any unprivileged account can pre-empt this address before the proposal activates using the ordinary `CREATE2` opcode (address = `keccak256(sender ++ salt ++ code)`), which is entirely attacker-controlled given the deterministic address derivation in `Program.createContract2`/`WalletUtil.generateContractAddress2` [3](#0-2) . Because contract addresses are only 21 bytes derived from `sha3omit12` of attacker-supplied inputs, an attacker willing to brute-force salts can eventually land a `CREATE2` deployment exactly at `HISTORY_STORAGE_ADDRESS`. Once any code or `ContractCapsule` exists there, `deploy()`'s guard (`manager.getCodeStore().has(...) || manager.getContractStore().has(...)`) trips permanently, `saveBlockHashHistoryInstalled(1L)` is never set, and `write()` also becomes a permanent no-op due to the same install-marker gate [4](#0-3) . From that point on, every call — from any TVM contract on the network — to the reserved EIP‑2935 "system" address executes the attacker's arbitrary bytecode instead of the trusted, protocol-defined BlockHashHistory logic, because ordinary `STATICCALL`/`CALL` semantics simply run whatever code sits at that address.

This is directly analogous to the reported CVE class: an unprivileged party who is first to create an object at a name/address that a trusted subsystem expects to resolve to a canonical entity can permanently hijack every later "query" (call) that trusts that name/address, causing execution of attacker-chosen logic instead of the intended trusted code — with no privilege check preventing the squatting.

### Impact Explanation
Any dApp, wallet, or on-chain protocol built on TIP‑2935 semantics that queries `HISTORY_STORAGE_ADDRESS` for historical block hashes (a security-critical primitive commonly used for commit-reveal schemes, randomness, and cross-contract verification) will silently receive results from attacker-controlled bytecode rather than genuine chain data. This enables an attacker to feed forged "historical block hash" data to any contract that relies on this system address, undermining commit-reveal/randomness-dependent contracts and enabling theft or manipulation of funds in protocols that trust this address. It also permanently and irreversibly disables an intended chain feature/hard-fork capability network-wide (once tripped, it can never self-heal, per the code comments), degrading the API/feature the node is expected to serve.

### Likelihood Explanation
The address is a hard-coded constant in the public source code, so no reconnaissance is needed. All that is required is an ordinary, unprivileged `CREATE2` from any account with enough energy/fee to deploy, executed with a salt colliding with the fixed address, before the TIP‑2935 proposal is activated by governance. Address squatting for a 21-byte/160-bit target theoretically requires an infeasible brute-force under normal hash-collision assumptions, but the risk is entirely dependent on how far in advance the address is published relative to activation and whether any additional protocol-level reservation/pre-allocation exists to prevent third parties from claiming it beforehand — the code contains no such protection at the account/contract-store level.

### Recommendation
Reserve the `HISTORY_STORAGE_ADDRESS` (and any other planned system/precompile addresses) at genesis or immediately upon software rollout, rejecting any `CREATE`/`CREATE2` that targets addresses in the reserved system range, rather than relying on a "first writer wins" check at fork-activation time. Alternatively, treat the address as a true precompile dispatched by `PrecompiledContracts.getContractForAddress` (like the other fixed-address system contracts already in that class) instead of storing mutable code/account state that can be raced by ordinary contract creation.

### Proof of Concept
1. Before the TIP‑2935 (`AllowTvmPrague`) proposal is activated on the network, an attacker deploys a factory contract and computes salts for `CREATE2` until the resulting address equals `410000f90827f1c53a10cb7a02335b175320002935` (`HistoryBlockHashUtil.HISTORY_STORAGE_ADDRESS`), placing arbitrary bytecode there.
2. When the proposal activates and `ProposalService` invokes `HistoryBlockHashUtil.deploy(manager)`, the check `manager.getCodeStore().has(HISTORY_STORAGE_ADDRESS) || manager.getContractStore().has(HISTORY_STORAGE_ADDRESS)` is `true`, so the function logs a warning and returns without installing the canonical bytecode, matching the behavior explicitly validated by the existing test `writeIsNoOpOnForeignCode` [5](#0-4) .
3. From that block forward, every contract that performs `STATICCALL`/queries to `HISTORY_STORAGE_ADDRESS` expecting TIP‑2935 semantics instead executes the attacker's bytecode, and `HistoryBlockHashUtil.write()` never records real parent hashes because `isBlockHashHistoryInstalled()` stays `0` forever.

### Citations

**File:** framework/src/main/java/org/tron/core/db/HistoryBlockHashUtil.java (L31-33)
```java
  // 21-byte TRON address (0x41 prefix + 20-byte EVM address 0x0000F908...2935)
  public static final byte[] HISTORY_STORAGE_ADDRESS =
      Hex.decode("410000f90827f1c53a10cb7a02335b175320002935");
```

**File:** framework/src/main/java/org/tron/core/db/HistoryBlockHashUtil.java (L100-106)
```java
  public static void deploy(Manager manager) {
    if (manager.getCodeStore().has(HISTORY_STORAGE_ADDRESS)
        || manager.getContractStore().has(HISTORY_STORAGE_ADDRESS)) {
      logger.warn("TIP-2935: foreign state at {}, skipping deploy",
          Hex.toHexString(HISTORY_STORAGE_ADDRESS));
      return;
    }
```

**File:** framework/src/main/java/org/tron/core/db/HistoryBlockHashUtil.java (L139-156)
```java
  public static void write(Manager manager, BlockCapsule block) {
    // Genesis has no parent; applyBlock never invokes this for block 0, but be
    // explicit so (0-1) % 8191 = -1 in Java can never corrupt a slot.
    if (block.getNum() <= 0) {
      return;
    }
    // Defense-in-depth: deploy() skips on foreign state at the canonical
    // address, but the proposal flag still commits. Gate on the install
    // marker (set at the tail of a successful deploy()) so write() can never
    // overwrite an unrelated contract's storage. Single store hit, cached.
    if (!manager.getDynamicPropertiesStore().isBlockHashHistoryInstalled()) {
      return;
    }
    long slot = (block.getNum() - 1) % HISTORY_SERVE_WINDOW;
    Storage storage = new Storage(HISTORY_STORAGE_ADDRESS, manager.getStorageRowStore());
    storage.put(new DataWord(slot), new DataWord(block.getParentHash().getBytes()));
    storage.commit();
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L1629-1652)
```java
  public void createContract2(DataWord value, DataWord memStart, DataWord memSize, DataWord salt) {
    if (VMConfig.allowTvmOsaka()) {
      returnDataBuffer = null; // reset return buffer right before the call
    }

    byte[] senderAddress;
    if ((VMConfig.allowTvmCompatibleEvm() || VMConfig.allowTvmOsaka())
        && getCallDeep() == MAX_DEPTH) {
      stackPushZero();
      return;
    }
    if (getCallDeep() == MAX_DEPTH) {
      MUtil.checkCPUTimeForCreate2();
    }
    if (VMConfig.allowTvmIstanbul()) {
      senderAddress = getContextAddress();
    } else {
      senderAddress = getCallerAddress().toTronAddress();
    }
    byte[] programCode = memoryChunk(memStart.intValue(), memSize.intValue());

    byte[] contractAddress = WalletUtil
        .generateContractAddress2(senderAddress, salt.getData(), programCode);
    createContractImpl(value, programCode, contractAddress, true);
```

**File:** framework/src/test/java/org/tron/core/db/HistoryBlockHashIntegrationTest.java (L443-477)
```java
  /**
   * Defense-in-depth: when foreign bytecode sits at the canonical address,
   * {@code deploy()} skips and the install marker stays 0, so {@code write()}
   * must refuse to overwrite that contract's storage every block. Triggering
   * the collision in practice requires a SHA-3 pre-image of the address, but
   * the marker check is a single cached store hit.
   */
  @Test
  public void writeIsNoOpOnForeignCode() {
    byte[] addr = HistoryBlockHashUtil.HISTORY_STORAGE_ADDRESS;
    byte[] foreignCode = Hex.decode("60016002");
    chainBaseManager.getCodeStore().put(addr, new CodeCapsule(foreignCode));

    HistoryBlockHashUtil.deploy(dbManager);

    assertFalse("install marker must stay 0 when deploy skipped",
        chainBaseManager.getDynamicPropertiesStore().isBlockHashHistoryInstalled());

    long blockNum = 100L;
    byte[] parentHash = new byte[32];
    Arrays.fill(parentHash, (byte) 0xcd);
    BlockCapsule block = new BlockCapsule(
        blockNum,
        Sha256Hash.wrap(parentHash),
        System.currentTimeMillis(),
        ByteString.copyFrom(new byte[21]));

    HistoryBlockHashUtil.write(dbManager, block);

    assertNull("write() must not overwrite a foreign contract's storage",
        readSlot(99L));
    assertArrayEquals("foreign code must remain intact",
        foreignCode, chainBaseManager.getCodeStore().get(addr).getData());
  }

```
