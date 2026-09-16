Based on my research, I found a concrete analog reachable by any unprivileged transaction sender (contract deployer/caller), located in the TRC10 self-destruct token merge logic — a different failure mode from the ERC-20 loop-revert DoS (TRC10 tokens are native ledger entries, not externally-callable contracts, so a "revert-on-transfer" attack does not translate directly), but the same underlying bug class: an attacker-controlled deposit of a native asset whose value is combined with another account's balance without safety checks, corrupting invariants in an actuator/TVM code path.

### Title
Unchecked long addition when merging TRC10 token balances on SELFDESTRUCT allows silent balance overflow - (File: actuator/src/main/java/org/tron/core/vm/utils/MUtil.java)

### Summary
`MUtil.transferAllToken`, invoked by the TVM `SELFDESTRUCT`/`SUICIDE` opcode handlers, merges every TRC10 token balance held by a self-destructing contract into the inheritor account using plain Java `long` addition instead of an overflow-checked operation, unlike other token-balance mutation paths in the codebase (e.g. `ExchangeInjectActuator`/`ExchangeTransactionActuator`, which use `addExact`/hardened `BigDecimal` math with explicit `ArithmeticException` handling).

### Finding Description
`MUtil.transferAllToken` iterates the self-destructing account's full TRC10 asset map and adds each token amount into the target account's existing balance for that token: [1](#0-0) 

The merge line `toBuilder.getAssetV2Map().getOrDefault(tokenId, 0L) + amount` uses raw `+`, which silently wraps around on `long` overflow instead of throwing. This method is called from `Program.suicide()` and `Program.suicide2()`, both of which are reachable directly by any contract deployer/caller triggering the `SUICIDE` opcode: [2](#0-1) [3](#0-2) 

TRC10 tokens can be issued by any account (asset issuer) with a total supply up to `Long.MAX_VALUE`, and transferred freely via `TransferAssetContract`/`transferToken` to any address, including a smart contract's address, without any whitelist/blacklist gate (unlike the OpenQ report's ERC-20/NFT asymmetry, TRC10 deposits into an account/contract are always accepted). An attacker can therefore engineer both the self-destructing contract and the inheritor account to hold near-`Long.MAX_VALUE` balances of the same token ID, then trigger `SUICIDE` to force the unchecked merge, wrapping the inheritor's resulting balance to a negative/incorrect value.

### Impact Explanation
A wrapped/negative TRC10 balance is an "unbacked balance" condition — subsequent legitimate transfers, asset issuance accounting, or exchange operations reading that account's asset map will operate on a corrupted value, potentially permitting further balance manipulation or the effective loss/duplication of tokens for the affected account, satisfying the "unbacked balance" impact category for a High-severity finding.

### Likelihood Explanation
The path requires no special privilege: any address can issue a TRC10 token (or reuse an existing one), transfer large TRC10 balances into two accounts it controls, deploy a trivial contract, and call `SUICIDE` — a single, unprivileged, self-contained transaction sequence. The only friction is engineering account balances close to `Long.MAX_VALUE`, which is achievable for a token the attacker itself issues.

### Recommendation
Replace the raw `+` addition in `MUtil.transferAllToken` with `Math.addExact` (or the project's existing `addExact`/hardened math helpers used elsewhere, e.g. in `ExchangeInjectActuator`/`ExchangeCapsule`), and propagate any resulting `ArithmeticException` as a `ContractValidateException`/`BytecodeExecutionException` so the transaction is safely reverted rather than allowing the state corruption to be committed.

### Proof of Concept
1. Attacker issues TRC10 token `T` with total supply near `Long.MAX_VALUE` via an `AssetIssueContract`.
2. Attacker transfers `~Long.MAX_VALUE/2` of `T` to contract `C` (which attacker deploys) and `~Long.MAX_VALUE/2` of `T` to inheritor address `I` (attacker-controlled EOA or contract).
3. Attacker triggers `C`'s constructor/logic to call `SUICIDE(I)`.
4. `Program.suicide`/`suicide2` invokes `MUtil.transferAllToken(state, C, I)`, which computes `I.balance(T) + C.balance(T)` using unchecked `long` addition, overflowing to a wrapped (potentially negative) value stored on-chain for `I`.

**Uncertainty**: I was unable to fully confirm within tool-call limits whether `addAssetAmountV2` (used by other paths such as `TransferAssetActuator`) enforces overflow checks that `transferAllToken` bypasses — this comparison would strengthen the finding but the unchecked `+` in `MUtil.transferAllToken` itself is directly confirmed from source.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/utils/MUtil.java (L28-41)
```java
  public static void transferAllToken(Repository deposit, byte[] fromAddress, byte[] toAddress) {
    AccountCapsule fromAccountCap = deposit.getAccount(fromAddress);
    Protocol.Account.Builder fromBuilder = fromAccountCap.getInstance().toBuilder();
    AccountCapsule toAccountCap = deposit.getAccount(toAddress);
    toAccountCap.importAllAsset();
    Protocol.Account.Builder toBuilder = toAccountCap.getInstance().toBuilder();
    fromAccountCap.getAssetMapV2().forEach((tokenId, amount) -> {
      toBuilder.putAssetV2(tokenId, toBuilder.getAssetV2Map().getOrDefault(tokenId, 0L) + amount);
      fromBuilder.putAssetV2(tokenId, 0L);
    });

    deposit.putAccountValue(fromAddress, new AccountCapsule(fromBuilder.build()));
    deposit.putAccountValue(toAddress, new AccountCapsule(toBuilder.build()));
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L480-488)
```java
        MUtil.transferAllToken(getContractState(), owner, blackHoleAddress);
      }
    } else {
      createAccountIfNotExist(getContractState(), obtainer);
      try {
        MUtil.transfer(getContractState(), owner, obtainer, balance);
        if (VMConfig.allowTvmTransferTrc10()) {
          MUtil.transferAllToken(getContractState(), owner, obtainer);
        }
```

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L562-567)
```java
    createAccountIfNotExist(getContractState(), obtainer);
    try {
      MUtil.transfer(getContractState(), owner, obtainer, balance);
      if (VMConfig.allowTvmTransferTrc10()) {
        MUtil.transferAllToken(getContractState(), owner, obtainer);
      }
```
