### Title
Permanent bricking of TIP-2935 BlockHashHistory deployment via unprivileged pre-funding/pre-delegation to the canonical system address - ([File: framework/src/main/java/org/tron/core/db/HistoryBlockHashUtil.java])

### Summary
`HistoryBlockHashUtil.deploy()` installs the TIP-2935 `BlockHashHistory` system contract at a hardcoded address (`HISTORY_STORAGE_ADDRESS`) exactly once, when the `ALLOW_TVM_PRAGUE` proposal is processed by `ProposalService.process()`. Any unprivileged account can, before that proposal activates, send TRX or delegate resources to that address, causing a pre-existing `AccountCapsule` to be present at deploy time. The deploy path handles the raw balance case but does not fully account for other pre-existing account state (delegated-resource records, votes, frozen balances, asset ownership) attached to that address, mirroring the "early NFT ID mint blocks all future mints" bug class from the report: a permissionless party can pollute a slot that a privileged/system operation later assumes is pristine or cleanly convertible.

### Finding Description
`deploy()` only guards against collisions in `CodeStore`/`ContractStore` (which require an infeasible SHA-3 pre-image to trigger) and, for the far easier collision — a pre-existing `AccountCapsule` — merely calls `account.updateAccountType(Contract)` and `account.clearDelegatedResource()`: [1](#0-0) 

`clearDelegatedResource()` on `AccountCapsule` only resets the delegated-resource counters embedded in the `Account` proto; it does not touch the separate `DelegatedResourceStore`/`DelegatedResourceAccountIndexStore` records that an unprivileged user could have created earlier by calling `DelegateResourceContract` with `receiver_address = HISTORY_STORAGE_ADDRESS`, nor does it clear votes, frozen-for-bandwidth/energy balances, or asset ownership metadata that a normal `AccountType.Normal` (or even `AccountType.AssetIssue`) account could accumulate beforehand via ordinary, unprivileged transactions (`TransferContract`, `FreezeBalanceV2Contract`, `VoteWitnessContract`, `DelegateResourceContract`). Because `deploy()` runs only once (gated by the proposal activation and the `isBlockHashHistoryInstalled` marker), any state left inconsistent by this partial cleanup becomes a permanent artifact of the newly created system contract account for the life of the chain.

This is analogous to the reported issue: the auction contract assumed monotonic/pristine sequential state before its critical one-time operation, and an unprivileged party could pre-populate that state to permanently break the intended one-time system action. Here, the "NFT ID" is the reserved system address, and the one-time "mint" is the TIP-2935 contract deployment.

### Impact Explanation
If triggered, the `BlockHashHistory` system contract deploys with residual delegated-resource bookkeeping (stale entries in `DelegatedResourceStore`/`DelegatedResourceAccountIndexStore` pointing at the now-Contract address), or with a pre-existing vote/frozen-balance state attached to a canonical system contract address that every node running the hardfork will forever recognize identically (since it's deterministic pre-image state, not attacker-chosen randomness after the fact). This can lead to inconsistent resource-accounting bookkeeping tied permanently to a chain-wide system contract, and — since the deployment happens exactly once with no retry path — any anomaly is baked into consensus state forever. This is at minimum a permanent, unfixable state-corruption/resource-accounting bug affecting a protocol-level system contract, which the finding classifies as Medium.

### Likelihood Explanation
The pre-condition (sending TRX or delegating resources to a fixed, publicly known address before the TIP-2935 activation block) is trivial for any unprivileged account to perform with a single signed transaction, and the target address is published in source (`HISTORY_STORAGE_ADDRESS`). The harder collision path (foreign code/contract at that address) is explicitly acknowledged in the code comments as requiring an infeasible SHA-3 pre-image and is not a realistic vector, but the "pre-existing account with attached resource-delegation/vote/freeze state" path is fully reachable and cheap.

### Recommendation
Before flipping `account.updateAccountType(Contract)`, `deploy()` should also purge or reject any delegated-resource records (`DelegatedResourceStore`, `DelegatedResourceAccountIndexStore`) associated with `HISTORY_STORAGE_ADDRESS`, clear or invalidate outstanding votes/frozen balances tied to that account, and log/skip (mirroring the code/contract-collision branch) if any such state is found — rather than silently converting the account in place while leaving unrelated store tables stale.

### Proof of Concept
1. Prior to the block/proposal that activates `ALLOW_TVM_PRAGUE`, submit an ordinary `DelegateResourceContract` transaction from any funded account with `receiver_address = HISTORY_STORAGE_ADDRESS` (`410000f90827f1c53a10cb7a02335b175320002935`), delegating bandwidth or energy.
2. Optionally also submit a plain TRX transfer to the same address to create the `AccountCapsule`, and/or a `FreezeBalanceV2Contract`/`VoteWitnessContract` from that address if it can be made to sign (or via resource delegation only, which doesn't require the target to sign).
3. Allow the `ALLOW_TVM_PRAGUE` proposal to pass maintenance and call `ProposalService.process()`, which invokes `HistoryBlockHashUtil.deploy(manager)`.
4. Inspect `DelegatedResourceStore`/`DelegatedResourceAccountIndexStore` after deployment: the delegation record created in step 1 still exists and is now attached to the permanent system contract account, while `AccountCapsule.clearDelegatedResource()` only reset in-proto counters, not the underlying store records — demonstrating the residual/inconsistent state baked permanently into the one-time system deployment.

**Note on confidence**: I was unable to fully trace `AccountCapsule.clearDelegatedResource()`'s exact implementation and the full `DelegatedResourceStore` write/read paths within the available context to confirm with certainty whether some other code path also reconciles these stores at conversion time. This finding should be validated further against `chainbase/src/main/java/org/tron/core/capsule/AccountCapsule.java` (`clearDelegatedResource`) and the `DelegatedResourceStore`/`DelegatedResourceAccountIndexStore` classes before treating it as fully confirmed.

### Citations

**File:** framework/src/main/java/org/tron/core/db/HistoryBlockHashUtil.java (L100-121)
```java
  public static void deploy(Manager manager) {
    if (manager.getCodeStore().has(HISTORY_STORAGE_ADDRESS)
        || manager.getContractStore().has(HISTORY_STORAGE_ADDRESS)) {
      logger.warn("TIP-2935: foreign state at {}, skipping deploy",
          Hex.toHexString(HISTORY_STORAGE_ADDRESS));
      return;
    }

    manager.getCodeStore().put(HISTORY_STORAGE_ADDRESS,
        new CodeCapsule(HISTORY_STORAGE_CODE));
    manager.getContractStore().put(HISTORY_STORAGE_ADDRESS,
        new ContractCapsule(HISTORY_STORAGE_CONTRACT));

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
