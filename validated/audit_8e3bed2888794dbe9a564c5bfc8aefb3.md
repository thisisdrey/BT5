### Title
Dust-transfer griefing fills `depositTokensOfAccount`/`debtTokensOfAccount` to `MAX_TOKENS_PER_USER`, permanently DoS-ing deposits, borrows, and inbound collateral transfers for a victim — (File: contracts/Pool.sol)

### Summary
`Pool` enforces a per-account cap of `MAX_TOKENS_PER_USER = 30` across the combined `depositTokensOfAccount` and `debtTokensOfAccount` sets. Any unprivileged user can push a victim's `depositTokensOfAccount` set to the cap by transferring 1 wei of each pool's `DepositToken` to them. Once full, `Pool.addToDepositTokensOfAccount` / `addToDebtTokensOfAccount` revert with `UserReachedMaxTokens`, which makes `DepositToken._transfer` / `_mint` and `DebtToken._mint` (issue/leverage) revert for that account. The victim cannot deposit new collateral, cannot receive depositToken transfers, and cannot open new debt positions — a reachable liveness/DoS analog of the crash/hang bug class, exploitable by any EOA.

### Finding Description
- `Pool.onlyIfAdditionWillNotReachMaxTokens` reverts when `debtTokensOfAccount.length(account_) + depositTokensOfAccount.length(account_) >= 30`. [1](#0-0) 
- `addToDepositTokensOfAccount` and `addToDebtTokensOfAccount` are the only mutators of the per-account sets and both apply the cap. [2](#0-1) 
- `DepositToken._transfer` adds the token to the recipient's list whenever the recipient's prior balance was zero and `amount_ > 0`; a dust `transfer(victim, 1)` from the attacker registers a new entry in the victim's set without any consent or opt-in. [3](#0-2) 
- The same add happens on `_mint` (i.e., `deposit`), so the victim also cannot deposit into any *new* `DepositToken` once at the cap. [4](#0-3) 
- `DebtToken` minting paths (`issue`, `leverage`, `flashIssue`) symmetrically call `addToDebtTokensOfAccount`, so borrowing any new synthetic asset is also bricked.
- Removal (`removeFromDepositTokensOfAccount`) only happens when a balance goes to zero, so a victim can only free a slot by fully emptying one of the dusted token balances — but they cannot transfer the dust out unless it is unlocked, and each dusted position counts against the cap while any nonzero balance remains.

### Impact Explanation
- Denial of deposits: a victim with an unhealthy or at-risk position cannot add a *new* collateral type to improve health before liquidation; if their existing collateral is insufficient, they are forced into liquidation — effectively freezing their ability to rescue the position.
- Denial of inbound transfers: any protocol or counterparty that pays the victim in `msdTOKEN`s (e.g., OTC settlement, refunds, `seize` proceeds routed to an account at cap) reverts.
- Denial of new debt issuance: `issue`/`leverage`/`SmartFarmingManager.leverage` revert when the minted `DebtToken` is not already in the victim's set.
- The attack costs the attacker only a dust deposit in each of the pool's `DepositToken`s (which are later withdrawable from their own account balance minus 1 wei). It is repeatable against any account, requires no privileges, no oracle manipulation, and no governance action, matching the CVE's "network-accessible, repeatable crash/hang (DoS)" bug class on Metronome's own code.

### Likelihood Explanation
The attack requires only that the victim's combined set has fewer than 30 entries and that the pool lists enough distinct `DepositToken`/`DebtToken` contracts to fill it. The attacker self-funds the dust by depositing minimal collateral per token. No modifier stops it: `transfer` is unauthenticated beyond `NotEnoughFreeBalance`/unlocked checks on the *sender*, and the cap check in `onlyIfAdditionWillNotReachMaxTokens` applies to the *recipient*, not the caller. The only practical mitigation is the number of registered tokens in the pool; if a pool has fewer than 30 registered tokens total the cap may not be reachable via depositTokens alone, but every `DebtToken` the attacker can cheaply `issue` to themselves does not help — debtTokens can't be transferred — so exploitability depends on the pool having enough depositToken markets, which is realistic on mainnet/Base deployments.

### Recommendation
Do not revert on the cap for inbound state changes the recipient didn't initiate. Options:
- Only apply `MAX_TOKENS_PER_USER` inside `DepositToken.deposit`/`DebtToken.issue` (user-initiated additions), and let unsolicited `transfer` additions bypass the cap; or
- Keep the cap for `deposit`/`issue` but allow transfers that would exceed it to skip registering in the set only if the position doesn't need it (risky for `depositOf` accounting — the set drives `depositOf`/`debtPositionOf`, so entries must always be registered). Preferred fix: revert only in `deposit`/`issue` when the sender is adding a *new* token to their own list, and remove the cap check from `addToDepositTokensOfAccount` when invoked via `_transfer`, or add a `depositAndAdd` flag distinguishing transfer vs. deposit.

### Proof of Concept
Hardhat sketch (based on the existing harness in `test/Pool.test.ts` and `test/DepositToken.test.ts`):

```ts
it('dust-transfer fills victim token list and bricks deposits', async () => {
  const victim = bob // attacker = alice
  const depositTokens: FakeContract[] = []

  // Attacker registers dust of every DepositToken into victim's account list
  for (let i = 0; i < 30; ++i) {
    const dt = await smock.fake('DepositToken')
    dt.underlying.returns(ethers.utils.hexlify(ethers.utils.randomBytes(20)))
    await pool.addDepositToken(dt.address) // governor setup; use real DepositTokens on fork
    depositTokens.push(dt)
  }

  // On a real deployment: attacker deposits 1 wei underlying into each DepositToken,
  // then calls depositToken.transfer(victim, 1) for each — each hits
  // Pool.addToDepositTokensOfAccount(victim) at contracts/Pool.sol:216.

  // Simulated end state:
  for (const dt of depositTokens) {
    await pool.connect(dt.wallet).addToDepositTokensOfAccount(victim.address)
  }

  // Victim can no longer receive any new depositToken:
  // _transfer -> addToDepositTokensOfAccount -> UserReachedMaxTokens
  const newDt = await smock.fake('DepositToken')
  newDt.underlying.returns(ethers.utils.hexlify(ethers.utils.randomBytes(20)))
  await pool.addDepositToken(newDt.address)
  await expect(pool.connect(newDt.wallet).addToDepositTokensOfAccount(victim.address))
    .revertedWithCustomError(pool, 'UserReachedMaxTokens')
})
```

On a mainnet/Base fork the loop uses real `DepositToken` instances (`pool.getDepositTokens()`), real `deposit(1, attacker)` + `transfer(victim, 1)` calls, and then demonstrates `deposit(...)` / `issue(...)` / `transfer(victim, x)` all reverting with `UserReachedMaxTokens` for the victim.

### Citations

**File:** contracts/Pool.sol (L143-148)
```text
    modifier onlyIfAdditionWillNotReachMaxTokens(address account_) {
        if (debtTokensOfAccount.length(account_) + depositTokensOfAccount.length(account_) >= MAX_TOKENS_PER_USER) {
            revert UserReachedMaxTokens();
        }
        _;
    }
```

**File:** contracts/Pool.sol (L204-220)
```text
    function addToDebtTokensOfAccount(address account_) external onlyIfAdditionWillNotReachMaxTokens(account_) {
        address _debtToken = _msgSender();
        _revertIfSenderIsNotDebtToken(_debtToken);
        if (!debtTokensOfAccount.add(account_, _debtToken)) revert DebtTokenAlreadyExists();
    }

    /**
     * @notice Add a deposit token to the per-account list
     * @dev This function is called from `DepositToken` when user's balance changes from `0`
     * @dev The caller should ensure to not pass `address(0)` as `_account`
     * @param account_ The account address
     */
    function addToDepositTokensOfAccount(address account_) external onlyIfAdditionWillNotReachMaxTokens(account_) {
        address _depositToken = _msgSender();
        _revertIfSenderIsNotDepositToken(_depositToken);
        if (!depositTokensOfAccount.add(account_, _depositToken)) revert DepositTokenAlreadyExists();
    }
```

**File:** contracts/DepositToken.sol (L485-488)
```text
        // Add this token to the deposit tokens list if the recipient is receiving it for the 1st time
        if (_balanceBefore == 0 && amount_ > 0) {
            pool.addToDepositTokensOfAccount(account_);
        }
```

**File:** contracts/DepositToken.sol (L517-520)
```text
        // Add this token to the deposit tokens list if the recipient is receiving it for the 1st time
        if (_recipientBalanceBefore == 0 && amount_ > 0) {
            pool.addToDepositTokensOfAccount(recipient_);
        }
```
