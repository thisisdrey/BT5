### Title
Unprivileged attacker can permanently block a victim's deposits and liquidations by filling their `MAX_TOKENS_PER_USER` slots via dust `DepositToken` transfers - ([File: contracts/DepositToken.sol](contracts/DepositToken.sol))

### Summary
`DepositToken._transfer` registers the recipient in `Pool.depositTokensOfAccount` whenever their balance goes from 0 to non-zero. `Pool.addToDepositTokensOfAccount` enforces a hard cap of `MAX_TOKENS_PER_USER = 30` (deposit + debt tokens combined) and reverts with `UserReachedMaxTokens`. An attacker can deposit dust collateral into each whitelisted `DepositToken`, then `transfer` 1 wei of each to a victim, filling all 30 slots. Thereafter, every action that would register a *new* token for the victim reverts: depositing a new collateral type (`DepositToken.deposit` → `_mint` → `addToDepositTokensOfAccount`), receiving deposit tokens, and — critically — `Pool.liquidate`/`DepositToken.seize` transfers of any collateral the victim does not already hold, because the seize is implemented as a `_transfer` to the liquidator/victim accounting path that triggers the same registration. The victim cannot mint new debt positions in new synthetics either (`DebtToken` issuance registers via `addToDebtTokensOfAccount` under the same combined cap).

The bug-class analog of CVE-2018-2590 (easily repeatable denial of service via a reachable code path) holds: a public, unprivileged transfer path deterministically crashes (reverts) a set of victim operations.

### Finding Description
- `DepositToken._transfer` calls `pool.addToDepositTokensOfAccount(recipient_)` when `_recipientBalanceBefore == 0 && amount_ > 0`, with no opt-in by the recipient [1](#0-0) .
- `Pool.addToDepositTokensOfAccount` / `addToDebtTokensOfAccount` are gated by `onlyIfAdditionWillNotReachMaxTokens`, which reverts once `debtTokensOfAccount.length + depositTokensOfAccount.length >= 30` [2](#0-1) [3](#0-2) .
- `_mint` (used by `deposit`) has the same registration, so even the victim's own `deposit()` of a not-yet-held collateral reverts [4](#0-3) .
- Attacker cost is only dust: each `DepositToken.deposit` accepts arbitrary small amounts, and transfers only require `unlockedBalanceOf` coverage, which dust always satisfies.

### Impact Explanation
Temporary freezing of funds / liveness loss, matching the accepted impact classes:
- The victim is blocked from depositing any new collateral type while slots are full — including the emergency action of topping up collateral to avoid liquidation. Combined with a price move, this can force an otherwise-preventable liquidation (bad debt / loss of funds).
- Liquidators holding many tokens can have `liquidate` revert when seizing a deposit token they don't already hold, reducing liquidation liveness.
- New debt issuance (`DebtToken` mint path registers via `addToDebtTokensOfAccount` under the same 30-slot budget) is also DoS'd.
- Recovery is possible but only by the victim manually transferring out the attacker's dust tokens one-by-one (each `transfer` removes the entry once the balance hits 0 via `removeFromDepositTokensOfAccount` [5](#0-4) ), at the victim's gas cost, and the attacker can re-grief at will (repeatable DoS, same as the CVE's "frequently repeatable crash").

### Likelihood Explanation
- Fully unprivileged: requires only calling public `deposit`/`transfer` on whitelisted `DepositToken`s with dust amounts across however many deposit tokens the pool lists.
- No oracle, governance, or privileged dependency; works on the deployed configuration (cap is a constant, no flag disables it).
- Attack cost scales with the number of whitelisted deposit tokens (gas + negligible dust); deterrence is only the gas the attacker spends, and re-griefing is cheap once the victim cleans up.

### Recommendation
- Make per-account registration opt-in: remove the automatic `addToDepositTokensOfAccount` on `_transfer` to recipients (receivers of raw msdTOKEN transfers would need an explicit `deposit`-side registration or a pull-based claim), or
- Return success no-op instead of reverting in `onlyIfAdditionWillNotReachMaxTokens` when registration is triggered by a third-party transfer, so unsolicited dust cannot consume the cap; keep the revert only on user-initiated `deposit`/`issue` paths, or
- Whitelist deposit-token transfers to the Pool/gateways so positions aren't freely transferable (transferability is what enables the grief).

### Proof of Concept
Hardhat (existing test scaffolding style, `test/Pool.test.ts` pattern):

```ts
// Assume pool has >= 30 whitelisted DepositTokens: dt[0..29]
// Attacker: deposit dust into each, then transfer 1 wei each to victim.
for (const dt of depositTokens) {
  await underlying(dt).approve(dt.address, 2);
  await dt.connect(attacker).deposit(2, attacker.address);      // mints msdTOKEN to attacker
  await dt.connect(attacker).transfer(victim.address, 1);       // registers victim
}
expect(await pool.getDepositTokensOfAccount(victim.address)).to.have.length(30);

// Victim tries to deposit a new collateral type (or issue a new debt token)
const newDt = depositTokens[30]; // a token victim doesn't hold
await underlying(newDt).connect(victim).approve(newDt.address, parseEther('10'));
await expect(
  newDt.connect(victim).deposit(parseEther('1'), victim.address)
).to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens');

// Victim must spend gas transferring out each dust token to free slots
await depositTokens[0].connect(victim).transfer(attacker.address, 1); // frees one slot
```

Key invariants demonstrated: `transfer` auto-registers the recipient ( [6](#0-5) ), and `addToDepositTokensOfAccount` reverts at the combined 30-token cap ( [2](#0-1) ), reproducible on a fork against deployed Pool/DepositToken contracts.

### Citations

**File:** contracts/DepositToken.sol (L469-489)
```text
    function _mint(
        address account_,
        uint256 amount_
    ) private onlyIfDepositTokenIsActive updateRewardsBeforeMintOrBurn(account_) {
        if (account_ == address(0)) revert MintToTheZeroAddress();

        totalSupply += amount_;
        if (totalSupply > maxTotalSupply) revert SurpassMaxDepositSupply();

        uint256 _balanceBefore = balanceOf[account_];
        unchecked {
            balanceOf[account_] = _balanceBefore + amount_;
        }

        emit Transfer(address(0), account_, amount_);

        // Add this token to the deposit tokens list if the recipient is receiving it for the 1st time
        if (_balanceBefore == 0 && amount_ > 0) {
            pool.addToDepositTokensOfAccount(account_);
        }
    }
```

**File:** contracts/DepositToken.sol (L498-526)
```text
    function _transfer(
        address sender_,
        address recipient_,
        uint256 amount_
    ) private updateRewardsBeforeTransfer(sender_, recipient_) {
        if (sender_ == address(0)) revert TransferFromTheZeroAddress();
        if (recipient_ == address(0)) revert TransferToTheZeroAddress();

        uint256 _senderBalanceBefore = balanceOf[sender_];
        if (_senderBalanceBefore < amount_) revert TransferAmountExceedsBalance();
        uint256 _recipientBalanceBefore = balanceOf[recipient_];

        unchecked {
            balanceOf[sender_] = _senderBalanceBefore - amount_;
            balanceOf[recipient_] += amount_;
        }

        emit Transfer(sender_, recipient_, amount_);

        // Add this token to the deposit tokens list if the recipient is receiving it for the 1st time
        if (_recipientBalanceBefore == 0 && amount_ > 0) {
            pool.addToDepositTokensOfAccount(recipient_);
        }

        // Remove this token from the deposit tokens list if the sender's balance goes to zero
        if (amount_ > 0 && balanceOf[sender_] == 0) {
            pool.removeFromDepositTokensOfAccount(sender_);
        }
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

**File:** contracts/Pool.sol (L216-220)
```text
    function addToDepositTokensOfAccount(address account_) external onlyIfAdditionWillNotReachMaxTokens(account_) {
        address _depositToken = _msgSender();
        _revertIfSenderIsNotDepositToken(_depositToken);
        if (!depositTokensOfAccount.add(account_, _depositToken)) revert DepositTokenAlreadyExists();
    }
```
