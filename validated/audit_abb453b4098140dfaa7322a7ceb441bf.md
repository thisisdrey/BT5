### Title
Attacker can permanently fill a victim's `MAX_TOKENS_PER_USER` slots with dust transfers, blocking new collateral deposits and forcing liquidation - ([File: contracts/Pool.sol])

### Summary
The on-chain analog of the yamux "unbounded pending-frames queue" is Metronome's per-account token lists: `debtTokensOfAccount` and `depositTokensOfAccount` in `Pool`. Every first-time receipt of a deposit token pushes the token into the victim's list, and `addToDepositTokensOfAccount`/`addToDebtTokensOfAccount` revert with `UserReachedMaxTokens` once `debtTokensOfAccount.length + depositTokensOfAccount.length >= MAX_TOKENS_PER_USER` (30). Any unprivileged user can force entries into a victim's list by dust-transferring deposit tokens (`DepositToken.transfer` → `_transfer` → `pool.addToDepositTokensOfAccount(recipient_)`) or by calling `deposit(amount_, victim_)` with a tiny amount. There is no opt-out: the victim cannot refuse incoming transfers, and freeing a slot requires fully zeroing a balance, which the attacker can immediately refill. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) 

### Finding Description
`Pool.addToDepositTokensOfAccount` enforces `onlyIfAdditionWillNotReachMaxTokens(account_)`, which reverts when the combined per-account lists reach `MAX_TOKENS_PER_USER = 30`. The set is populated automatically and involuntarily: `DepositToken._transfer` calls `pool.addToDepositTokensOfAccount(recipient_)` whenever the recipient's prior balance is zero and `amount_ > 0`, and `DepositToken._mint` does the same on `deposit(amount_, account_)`. The same mechanism exists for debt tokens via `DebtToken` issuance (`addToDebtTokensOfAccount`).

An attacker who holds dust balances of the pool's deposit tokens (obtainable by depositing a minimal amount of each collateral, or buying the msd tokens) can transfer `1 wei` of each deposit token to a target account. Each transfer permanently occupies one slot in `depositTokensOfAccount[victim]` because the attacker keeps a residual balance and can re-add the token after the victim zeroes it. Once the victim's combined list reaches 30 entries:

- `DepositToken.deposit(amount_, victim)` for any *new* collateral type reverts inside `_mint` → `addToDepositTokensOfAccount` → `UserReachedMaxTokens`.
- `DebtToken.issue`/`mint` for any *new* synthetic asset reverts the same way, blocking the victim from issuing debt.
- `DepositToken.seize(from, victim, ...)` during liquidation reverts if the seized deposit token is not already in the victim's list — `seize` routes through `_transfer`, so a liquidator seizing a collateral type the liquidator's chosen `to_` address doesn't already hold also reverts with `UserReachedMaxTokens`.

The victim's only escape is fully withdrawing/transferring some token to zero to free a slot, but `withdraw` requires the amount to be unlocked (`_revertIfLocked` via `unlockedBalanceOf`), so an underwater or near-liquidation victim cannot free slots from locked collateral, and any freed slot can be instantly refilled by the attacker for ~1 wei + gas.

### Impact Explanation
Temporary freezing of funds and forced liquidation: a victim whose collateral is near the liquidation threshold cannot add a *new* collateral type to restore health — every `deposit` of an unheld deposit token reverts. An attacker can therefore hold a targeted position's health factor down and liquidate it themselves for the liquidator fee (`Pool.liquidate` → `DepositToken.seize` to their own prepared address). The griefing also blocks receiving liquidation proceeds or msd token transfers of new types. Cost to the attacker is dust balances of each deposit token plus gas, and the block is sustainable indefinitely since slots refill automatically. The invariant broken is liveness/position-management: `MAX_TOKENS_PER_USER` was designed as a gas-safety bound but, because list entries are pushable by anyone via transfers, it becomes an attacker-controlled per-account resource exhaustion identical in spirit to the unbounded yamux queue — the victim's "queue" (token list) is filled by remote-triggered entries they cannot reject.

### Likelihood Explanation
- Fully permissionless: `DepositToken.transfer`/`transferFrom` and `deposit(amount_, account_)` are public; no privileged role needed.
- Pool deposit tokens are capped at `MAX_TOKENS_PER_USER` themselves (`addDepositToken` reverts `ReachedMaxDepositTokens`), so filling a victim's deposit-token slots requires at most ~N transfers where N is the number of live collaterals — cheap on any chain.
- Attack requires the victim to be at or approaching 30 combined entries; for accounts already holding several tokens, only a handful of dust transfers are needed. For a fresh victim account the attacker needs up to 30 dust transfers (still trivially cheap), but is limited by how many deposit/debt tokens actually exist in the pool — typically fewer than 30, which is the main mitigant: the attack fully blocks only when live deposit tokens + victim's debt tokens ≥ 30. Pools with many listed collaterals (the cap exists precisely because governance can list up to 30) are fully exploitable.
- Not stopped by existing guards: `ReentrancyGuardTransient`, `onlyIfDepositTokenExists`, `whenNotPaused`, and `_revertIfLocked` all pass for dust transfers; `SynthContext._msgSender` resolves to the attacker EOA normally.

### Recommendation
Do not let involuntary receipt consume a scarce slot. Options:

- Increase robustness of the cap by only counting tokens above a minimum balance threshold, or track list membership lazily (iterate `depositTokens`/`debtTokens` global sets in `depositOf`/`debtOf` instead of per-account sets), removing the per-account cap entirely.
- Alternatively, allow `addTo*TokensOfAccount` to succeed beyond the cap when called from `seize`/liquidation paths, and let `deposit` of a new token for a capped account skip list insertion while still crediting balance (with a documented caveat that `depositOf` may undercount — prefer the lazy-iteration redesign).
- Short-term mitigation: keep the number of listed deposit tokens well below `MAX_TOKENS_PER_USER` so victims retain headroom, and document that users should monitor their token lists.

### Proof of Concept
Hardhat-style sketch (matches existing test harness in `test/Pool.test.ts` using smock fakes; on a fork, use real `DepositToken`s):

```ts
// Assume pool has >= N deposit tokens listed such that
// N depositTokens + victimDebtTokens >= MAX_TOKENS_PER_USER (30).
const max = (await pool.MAX_TOKENS_PER_USER()).toNumber();
const depositTokens: DepositToken[] = await Promise.all(
  (await pool.getDepositTokens()).map(a => ethers.getContractAt('DepositToken', a))
);

// 1) Attacker acquires dust of each deposit token
for (const dt of depositTokens) {
  const underlying = await ethers.getContractAt('ERC20', await dt.underlying());
  await underlying.connect(attacker).approve(dt.address, 2);
  await dt.connect(attacker).deposit(2, attacker.address); // mints msdToken to attacker
}

// 2) Attacker fills victim's slots with 1-wei transfers until cap reached
for (const dt of depositTokens) {
  const len = (await pool.getDepositTokensOfAccount(victim.address)).length
            + (await pool.getDebtTokensOfAccount(victim.address)).length;
  if (len >= max) break;
  await dt.connect(attacker).transfer(victim.address, 1); // pushes dt into victim's list
}
expect(
  (await pool.getDepositTokensOfAccount(victim.address)).length
+ (await pool.getDebtTokensOfAccount(victim.address)).length
).to.eq(max);

// 3) Victim cannot deposit a collateral type not already held
const newDt = depositTokens.find(d => /* not previously held by victim */ true)!;
const und = await ethers.getContractAt('ERC20', await newDt.underlying());
await und.connect(victim).approve(newDt.address, parseEther('10'));
await expect(newDt.connect(victim).deposit(parseEther('10'), victim.address))
  .to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens');

// 4) Victim cannot free a slot while collateral is locked by debt:
//    withdraw of the full dust balance reverts via _revertIfLocked,
//    and even if freed, attacker re-adds it with another 1-wei transfer.

// 5) Victim's position becomes unhealthy (price move) -> cannot top up
//    with a new collateral -> attacker liquidates:
await pool.connect(attacker).liquidate(
  victim.address, depositTokenHeldByVictim, syntheticToken, amountToRepay
); // succeeds; seize goes to attacker's own prepared address
```

Key code path: `DepositToken._transfer` → `pool.addToDepositTokensOfAccount(recipient_)` → `onlyIfAdditionWillNotReachMaxTokens` → `revert UserReachedMaxTokens()`, and `DepositToken._mint` (via `deposit`) hits the same revert.

### Citations

**File:** contracts/Pool.sol (L79-79)
```text
    uint256 public constant MAX_TOKENS_PER_USER = 30;
```

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

**File:** contracts/DepositToken.sol (L517-525)
```text
        // Add this token to the deposit tokens list if the recipient is receiving it for the 1st time
        if (_recipientBalanceBefore == 0 && amount_ > 0) {
            pool.addToDepositTokensOfAccount(recipient_);
        }

        // Remove this token from the deposit tokens list if the sender's balance goes to zero
        if (amount_ > 0 && balanceOf[sender_] == 0) {
            pool.removeFromDepositTokensOfAccount(sender_);
        }
```
