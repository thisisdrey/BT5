### Title
Pre-fundable canonical address silently seized and converted to a System Contract, absorbing attacker-controlled balance/asset/permission state at TIP-2935 activation — (File: `framework/src/main/java/org/tron/core/db/HistoryBlockHashUtil.java`)

### Summary
`HistoryBlockHashUtil.deploy()` implements a `DiskOrCreate`-style pattern: it checks only whether *code/contract metadata* already exists at the canonical address `HISTORY_STORAGE_ADDRESS`, and if not, proceeds to install the system contract there — regardless of whether an *account* (EOA) already exists at that exact address. [1](#0-0)  If an account already exists (e.g. because someone transferred TRX to it before activation), the code takes it over in place — `updateAccountType(Contract)` + `clearDelegatedResource()` — instead of rejecting the deploy or validating that the pre-existing state was not attacker-planted. [2](#0-1) 

### Finding Description
`HISTORY_STORAGE_ADDRESS` is a fixed, publicly known 21-byte address (`0x410000f90827f1c53a10cb7a02335b175320002935`). [3](#0-2)  Because it is deterministic and public, **any unprivileged transaction broadcaster can pre-fund it** with a plain `TransferContract` before the TIP-2935 proposal activates, the same way `TransferActuator` auto-creates a normal account for any never-before-seen `toAddress`. [4](#0-3) 

When the proposal later activates, `ProposalService`/`Manager` calls `HistoryBlockHashUtil.deploy()` exactly once. [5](#0-4)  The guard at the top of `deploy()` only checks `codeStore`/`contractStore` — i.e., "is there already a contract here?" — not whether an *account* with attacker-supplied balance/assets/frozen-resources already sits at that address. [1](#0-0)  The `else` branch that runs when the account already exists mutates it in place, preserving whatever balance, TRC10 asset balances, votes, or (pre-TIP) permission data the attacker had already written to that account, and only clears delegated resources and flips the account type to `Contract`. [2](#0-1) 

This is structurally identical to the KubeVirt `DiskOrCreate` bug: the "create if it doesn't already exist" code path assumes freshly-created state is safe to grant elevated status to (there, file ownership set to the privileged `qemu` UID; here, account identity absorbed into a privileged system contract address that other contracts trust via `STATICCALL` reads of on-chain block-hash history), without validating that the object was not pre-seeded by an unprivileged actor. In both cases the root cause is the same logic gap: existence-check scoped too narrowly (file/contract existence) instead of covering the full resource (file *or its metadata*/account *and* its balance-bearing state), letting attacker-controlled pre-existing state be silently absorbed and re-labeled as privileged/system state.

### Impact Explanation
An attacker who front-runs the TIP-2935 activation by sending TRX (and/or TRC10 assets, votes, frozen balance) to the canonical address can have that balance and state permanently baked into the "official" `BlockHashHistory` system contract's account record. Depending on how downstream code treats accounts at that address (e.g., balance accounting, asset supply bookkeeping, `AccountStore` invariants for `Contract`-typed accounts), this can result in unbacked/duplicated balance association with a system contract, corruption of account-type invariants relied upon elsewhere in the codebase (e.g., `RepositoryImpl`/actuators assuming `Contract`-type accounts never hold externally-transferred assets or votes prior to code deployment), and a permanently non-reproducible/inconsistent state depending on whether an attacker happened to pre-fund the address before the single, one-time deploy() execution. This is a chain-consensus-relevant condition because `deploy()` runs during block/maintenance processing in `Manager`, and its outcome (`accountExisting` branch taken or not) is deterministic across all nodes only if all nodes see the same pre-funding transaction — which they will, since it's an ordinary broadcast transaction — but the resulting account state permanently diverges from the intended "clean" system contract deployment.

### Likelihood Explanation
High feasibility: the target address is hardcoded and public in the codebase, requires no special privilege to fund (a plain `TransferContract` or asset transfer suffices, exactly like the `TransferActuator`/`TransferAssetActuator` auto-create-account paths already exercised in the test suite), and the attacker only needs to send the transaction before the TIP-2935 proposal activation timestamp, which is public/predictable network state. No signature forgery, no malicious SR/witness, and no special node access are required — an ordinary account holder is sufficient.

### Recommendation
In `HistoryBlockHashUtil.deploy()`, treat any pre-existing account at `HISTORY_STORAGE_ADDRESS` the same as pre-existing code: log and skip the deploy (do not flip `saveBlockHashHistoryInstalled(1L)`), or explicitly zero/quarantine the account's balance, asset map, votes, and frozen resources before re-typing it to `Contract`, so that no externally-transferred value or state survives the conversion into a system contract identity. At minimum, treat the "pre-existing account" branch with the same "foreign state" rejection semantics currently applied to `codeStore`/`contractStore` collisions.

### Proof of Concept
1. Attacker (unprivileged) broadcasts a standard `TransferContract` sending TRX (or `TransferAssetContract` sending a TRC10 asset, or freezes for bandwidth/energy to build up delegated/frozen resource, or votes) to the address `0x410000f90827f1c53a10cb7a02335b175320002935` (`HISTORY_STORAGE_ADDRESS`) before TIP-2935 activation — this succeeds via the ordinary auto-create-account path in `TransferActuator.execute()`. [4](#0-3) 
2. When the proposal activates, `Manager`/`ProposalService` calls `HistoryBlockHashUtil.deploy(manager)`.
3. `deploy()` finds no code/contract at the address (guard passes), but finds the attacker-funded `AccountCapsule`; it takes the `accountExisting` branch, converting it to `AccountType.Contract` and clearing only delegated resources — preserving the attacker's balance/assets/votes into the newly labeled system contract account. [2](#0-1) 
4. The resulting on-chain state permanently associates attacker-supplied value with the canonical system contract address, which every node processing the same transaction history will replicate identically, entrenching the inconsistency into consensus state.

### Citations

**File:** framework/src/main/java/org/tron/core/db/HistoryBlockHashUtil.java (L31-33)
```java
  // 21-byte TRON address (0x41 prefix + 20-byte EVM address 0x0000F908...2935)
  public static final byte[] HISTORY_STORAGE_ADDRESS =
      Hex.decode("410000f90827f1c53a10cb7a02335b175320002935");
```

**File:** framework/src/main/java/org/tron/core/db/HistoryBlockHashUtil.java (L80-99)
```java
  /**
   * Deploy the TIP-2935 BlockHashHistory contract at {@code HISTORY_STORAGE_ADDRESS}.
   * If foreign code or contract metadata already sits at the canonical address,
   * logs a warning and returns without writing — the collision is deterministic
   * across nodes (same pre-state ⇒ same decision), so the proposal flag still
   * commits and chain consensus is intact. The foreign contract executes as-is
   * on every node; TIP-2935 functionality is silently absent at this address.
   * A SHA-3 pre-image of the address is the only realistic way that branch
   * fires, so it's belt-and-braces. A pre-existing non-contract account at the
   * address is the common case (anyone can transfer TRX there to activate it
   * as an EOA), so we upgrade its type to {@code Contract} in place — matching
   * the CREATE2 collision branch ({@code updateAccountType} +
   * {@code clearDelegatedResource}) and preserving balance/asset state.
   *
   * <p>Called only from {@code ProposalService} inside maintenance-time block
   * processing. Proposal validation rejects re-activation, so this runs at most
   * once per chain history; the three store writes share the block's revoking
   * session, so any node-local exception (RocksDB / IO) propagates and rolls
   * the {@code saveAllowTvmPrague(1)} write back atomically.
   */
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

**File:** framework/src/main/java/org/tron/core/db/HistoryBlockHashUtil.java (L113-121)
```java
    AccountCapsule account = manager.getAccountStore().get(HISTORY_STORAGE_ADDRESS);
    boolean accountExisting = account != null;
    if (!accountExisting) {
      account = new AccountCapsule(HISTORY_STORAGE_ACCOUNT);
    } else {
      account.updateAccountType(Protocol.AccountType.Contract);
      account.clearDelegatedResource();
    }
    manager.getAccountStore().put(HISTORY_STORAGE_ADDRESS, account);
```

**File:** actuator/src/main/java/org/tron/core/actuator/TransferActuator.java (L48-58)
```java
      // if account with to_address does not exist, create it first.
      AccountCapsule toAccount = accountStore.get(toAddress);
      if (toAccount == null) {
        boolean withDefaultPermission =
            dynamicStore.getAllowMultiSign() == 1;
        toAccount = new AccountCapsule(ByteString.copyFrom(toAddress), AccountType.Normal,
            dynamicStore.getLatestBlockHeaderTimestamp(), withDefaultPermission, dynamicStore);
        accountStore.put(toAddress, toAccount);

        fee = fee + dynamicStore.getCreateNewAccountFeeInSystemContract();
      }
```
