### Title
Attacker can fill a victim's per-account token lists via dust `DepositToken` transfers, DoS-ing new borrows and collateral deposits - ([File: contracts/Pool.sol])

### Summary
`Pool` enforces a hard cap `MAX_TOKENS_PER_USER` on the combined length of `debtTokensOfAccount` and `depositTokensOfAccount`. Whenever a `DepositToken` balance goes from `0` to `>0` — including via a plain ERC20 `transfer` — `Pool.addToDepositTokensOfAccount` is invoked and reverts with `UserReachedMaxTokens` once the victim is at the cap. An unprivileged attacker can deposit dust into every registered collateral, receive the msdTokens, and `transfer` 1 wei of each to a victim, permanently occupying all of the victim's deposit-token slots. Once full, any operation that would add a new token to the victim's list reverts.

### Finding Description
- `Pool.sol:143-148` — `onlyIfAdditionWillNotReachMaxTokens` reverts `UserReachedMaxTokens` when `debtTokensOfAccount.length(account_) + depositTokensOfAccount.length(account_) >= MAX_TOKENS_PER_USER`. [1](#0-0) 
- `Pool.sol:216-220` — `addToDepositTokensOfAccount` applies that modifier before inserting into the `MappedEnumerableSet`. [2](#0-1) 
- `DepositToken.sol:518-520` — `_transfer` calls `pool.addToDepositTokensOfAccount(recipient_)` whenever the recipient's prior balance was `0`. A dust `transfer` therefore fills a slot on the victim with no victim consent. [3](#0-2) 
- `DepositToken.sol:486-488` — `_mint` does the same on `deposit(amount_, onBehalfOf_)`, so a maxed victim cannot receive a new collateral deposit or be deposited on behalf of. [4](#0-3) 
- `DepositToken.sol:523-525` — slots are only freed when the holder's balance returns to `0` via `transfer`/`withdraw`/`seize`.

The same cap gates `DebtToken` mints (`DebtToken` calls `addToDebtTokensOfAccount` from its mint path), so `issue`/`leverage`/`SmartFarmingManager.leverage` for any debt token the victim does not already hold reverts.

### Impact Explanation
Denial of service against a targeted account, matching the CVE class (availability). A maxed-out victim:

- cannot mint any *new* synthetic debt token (`issue`, `leverage`, cross-chain leverage all revert at `addToDebtTokensOfAccount`),
- cannot receive a `DepositToken` they do not already hold (transfers to them revert), so if governance later registers a new collateral, the victim is permanently locked out of it,
- cannot be the `onBehalfOf_` recipient of a deposit into a new collateral type.

Recovery is not always free: slots are only released when a balance hits `0`, and `DepositToken.transfer`/`withdraw` are gated by `_revertIfLocked`/`unlockedBalanceOf` (`DepositToken.sol:348-353, 383-398`). A victim whose debt fully locks their collateral (`_issuableInUsd == 0` ⇒ `unlockedBalanceOf == 0`) cannot transfer the dust out at all until they first repay debt — and the attacker can re-dust the freed slot in the same block, keeping the victim grieved indefinitely at dust cost.

### Likelihood Explanation
- Fully unprivileged: attacker needs only ERC20 collateral tokens and public `deposit`/`transfer` calls; no governor, keeper, oracle, or bridge role involved.
- Cost is bounded by dust deposits into each registered `DepositToken` (number of registered tokens is itself capped by `MAX_TOKENS_PER_USER` per `Pool.sol:703`), plus per-transfer gas. Re-dusting to counter victim cleanup is cheap and repeatable.
- Effective whenever `registeredDepositTokens + victimDebtTokens ≥ MAX_TOKENS_PER_USER`, which is reachable in pools with many collateral types or a low `MAX_TOKENS_PER_USER`, and always blocks future collateral types once the cap is hit.

### Recommendation
- Do not add a token to `depositTokensOfAccount` on incoming `transfer`/`seize` — or allow removals independent of the cap (already satisfied) *and* make additions on receipt opt-out-able, e.g. track set membership only for tokens added via `deposit`, treating transferred-in balances separately.
- Alternatively, let users purge entries themselves: add a `Pool.removeDepositTokenFromMyAccount(depositToken_)` callable by the account when its balance is dust/zero, or make `_revertIfLocked` ignore balances below a small threshold so dust can always be shed.
- At minimum, exclude received dust from counting toward `MAX_TOKENS_PER_USER` by only enforcing the cap on `deposit`/`issue`, not on unsolicited inbound transfers.

### Proof of Concept
Hardhat sketch (fork a deployment, e.g. `deployments/mainnet`):

```ts
// setup: pool, msdTokens[] = all registered DepositTokens, victim = alice
const attacker = bob;

// 1. Attacker deposits dust into every DepositToken to obtain transferable msdTOKENs
for (const dt of depositTokens) {
  const underlying = await ethers.getContractAt('ERC20', await dt.underlying());
  await underlying.connect(attacker).approve(dt.address, ethers.constants.MaxUint256);
  await dt.connect(attacker).deposit(1n, attacker.address); // 1 wei
}

// 2. Fill victim's deposit-token slots via unsolicited transfers
for (const dt of depositTokens) {
  await dt.connect(attacker).transfer(victim.address, 1n); // adds to victim's list
}
// victim's debtTokensOfAccount + depositTokensOfAccount == MAX_TOKENS_PER_USER
expect(await pool.getDepositTokensOfAccount(victim.address)).to.have.lengthOf(depositTokens.length);

// 3. DoS 1: victim cannot issue a synth whose DebtToken they don't already hold
await expect(
  debtToken.connect(victim).issue(victim.address, 1n) // via pool issue path
).to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens');

// 4. DoS 2: any transfer of a NEW DepositToken to victim reverts
const newDt = newDepositToken; // registered later by governor
await expect(newDt.connect(attacker).transfer(victim.address, 1n))
  .to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens');

// 5. Persistence: victim's unlockedBalanceOf == 0 while fully borrowed,
//    so they cannot transfer the dust out without repaying debt first.
expect(await depositTokens[0].unlockedBalanceOf(victim.address)).to.eq(0);
```

Caveats not fully verified within iteration limits: the exact `MAX_TOKENS_PER_USER` constant value and `DebtToken` non-transferability were inferred from `Pool.sol` and test fixtures rather than read directly; if `MAX_TOKENS_PER_USER` far exceeds the number of registered deposit tokens on the target deployment, the attack only becomes effective after additional collateral types are listed.

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

**File:** contracts/Pool.sol (L216-220)
```text
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
