### Title
Unbounded forced growth of a victim's per-account deposit-token list via dust `deposit`/`transfer` permanently blocks new collateral positions — ([File: contracts/Pool.sol](contracts/Pool.sol))

### Summary
Analogous to GHSA-7ppr-r889-mcf2 (unbounded aggregation → DoS), `Pool` aggregates per-account deposit-token memberships in `depositTokensOfAccount` and enforces `MAX_TOKENS_PER_USER = 30` inside `addToDepositTokensOfAccount`. Membership is added without the recipient's consent whenever a `DepositToken` balance goes from 0 to positive — including via `DepositToken.deposit(amount, onBehalfOf)` where anyone can pick the beneficiary, and via `transfer`/`transferFrom`/`seize`. An unprivileged attacker can force-add dust balances of every registered deposit token to a victim, filling the list so that any subsequent mint/transfer/seize of a *new* deposit token to that account reverts with `UserReachedMaxTokens`.

### Finding Description
- `DepositToken._mint` adds the token to the recipient's per-account list whenever `_balanceBefore == 0`, with no opt-in from the recipient [1](#0-0) .
- `DepositToken._transfer` does the same for recipients [2](#0-1) .
- `DepositToken.deposit` lets any caller specify an arbitrary `onBehalfOf_`, only rejecting `address(0)` and the treasury [3](#0-2) .
- `Pool.addToDepositTokensOfAccount` enforces `debtTokensOfAccount + depositTokensOfAccount >= 30 → UserReachedMaxTokens` [4](#0-3) .

Because `_mint` performs `addToDepositTokensOfAccount` *after* the balance write, the revert poisons the whole transaction: deposits, transfers, and liquidation `seize`s of a token not already in the victim's list all fail once the list is full.

### Impact Explanation
- A victim whose list is filled cannot deposit any *new* collateral type and cannot receive transfers or liquidation-seized amounts of tokens not already in their list.
- Recovery requires the victim to fully burn each forced dust balance (`withdraw`) — up to 30 transactions — and if the withdraw fee exceeds the dust amount the victim eats a loss or the attacker can re-dust immediately after cleanup (persistent griefing at near-zero cost, since deposits mint at ~1:1 and the attacker keeps the underlying exposure only while tokens sit).
- Griefing of liquidation flows: if a known liquidator address (or SmartFarmingManager-driven flow minting to a fixed account) reaches the cap, `seize`/`_transfer` to it reverts, reverting the entire `Pool.liquidate` — temporary freezing of the liquidation path for positions paying out in new collateral tokens.

### Likelihood Explanation
- Fully unprivileged: `deposit(amount, victim)` and `transfer(victim, dust)` are public; no privileged role, oracle manipulation, or front-running assumption needed.
- Bounded by the number of registered deposit tokens (governor-capped at 30), so feasibility depends on the pool listing enough collateral; on deployed pools with several collaterals the attacker only needs to cover the gap between the victim's current list size and 30.
- Not stopped by any modifier: `nonReentrant`, `whenNotPaused`, and `SynthContext` checks do not restrict beneficiary choice or require recipient consent.

### Recommendation
- Remove per-account token-list entries on transfer/burn regardless of beneficiary, or gate `addToDepositTokensOfAccount` so that only mints initiated by the account itself (or with its consent) add entries — e.g., only track tokens the account explicitly deposits, or drop the `UserReachedMaxTokens` revert for unsolicited inbound transfers and let `debtPositionOf` iterate over registered tokens instead.

### Proof of Concept
Hardhat sketch (Foundry equivalent is straightforward):

```ts
// victim already holds some deposit tokens; attacker fills the rest of the list
const depositTokens: DepositToken[] = [...]; // all pool-registered collaterals
for (const dt of depositTokens) {
  const underlying = await ethers.getContractAt('ERC20', await dt.underlying());
  await underlying.mint(attacker.address, 1);
  await underlying.connect(attacker).approve(dt.address, 1);
  // force-adds `dt` to depositTokensOfAccount[victim] without victim consent
  await dt.connect(attacker).deposit(1, victim.address);
}
// now: depositTokensOfAccount[victim].length + debtTokensOfAccount[victim].length == 30

// any deposit of a collateral not yet in the victim's list reverts
await expect(
  newDepositToken.connect(victim).deposit(amount, victim.address)
).to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens');

// likewise, liquidation seizing a not-yet-listed collateral to a liquidator
// whose list was filled reverts inside DepositToken._transfer -> Pool.liquidate fails
```

Victim-side cleanup requires a `withdraw` (burn to zero) per forced token; the attacker can re-dust after each cleanup to sustain the block.

### Citations

**File:** contracts/DepositToken.sol (L211-234)
```text
    function deposit(
        uint256 amount_,
        address onBehalfOf_
    ) external override whenNotPaused nonReentrant onlyIfDepositTokenExists returns (uint256 _deposited, uint256 _fee) {
        if (amount_ == 0) revert AmountIsZero();
        if (onBehalfOf_ == address(0)) revert BeneficiaryIsNull();

        IPool _pool = pool;
        IERC20 _underlying = underlying;
        address _msgSender = _msgSender();
        address _treasury = address(_pool.treasury());

        if (_msgSender == _treasury) revert TreasuryCanNotDeposit();

        uint256 _balanceBefore = _underlying.balanceOf(_treasury);
        _underlying.safeTransferFrom(_msgSender, _treasury, amount_);
        amount_ = _underlying.balanceOf(_treasury) - _balanceBefore;

        (_deposited, _fee) = quoteDepositOut(amount_);
        if (_fee > 0) {
            _mint(_pool.feeCollector(), _fee);
        }

        _mint(onBehalfOf_, _deposited);
```

**File:** contracts/DepositToken.sol (L485-489)
```text
        // Add this token to the deposit tokens list if the recipient is receiving it for the 1st time
        if (_balanceBefore == 0 && amount_ > 0) {
            pool.addToDepositTokensOfAccount(account_);
        }
    }
```

**File:** contracts/DepositToken.sol (L517-520)
```text
        // Add this token to the deposit tokens list if the recipient is receiving it for the 1st time
        if (_recipientBalanceBefore == 0 && amount_ > 0) {
            pool.addToDepositTokensOfAccount(recipient_);
        }
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
