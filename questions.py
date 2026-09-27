import json
import os

from decouple import config

# todo: if scope_files is: 500 > 50, 300 > 30 , 100 > 10
MAX_REPO = 12
# todo: the GitLab namespace/project path, for example group/project
SOURCE_REPO = 'autonomoussoftware/metronome-synth-public'
# todo: the name of the repository
REPO_NAME = 'metronome-synth-public'

run_number = os.environ.get('GITHUB_RUN_NUMBER', '0')


def get_cyclic_index(run_number, max_index=100):
    """Convert run number to a cyclic index between 1 and max_index"""
    return (int(run_number) - 1) % max_index + 1


def load_repository_urls():
    """Load repository URLs from repositories.json."""
    repo_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "repositories.json")
    if not os.path.exists(repo_file):
        return []

    try:
        with open(repo_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return []

    if not isinstance(data, list):
        return []

    return [url for url in data if isinstance(url, str) and url.strip()]


if run_number == "0":
    BASE_URL = f"https://deepwiki.com/{SOURCE_REPO}"
else:
    repository_urls = load_repository_urls()
    if repository_urls:
        run_index = get_cyclic_index(run_number, len(repository_urls))
        BASE_URL = repository_urls[run_index - 1]
    else:
        BASE_URL = f"https://deepwiki.com/{SOURCE_REPO}"

scope_files = [
    # =================================================================================
    # Identity and access: Operator batching, SynthContext sender resolution, roles
    # =================================================================================
    "contracts/Operator.sol",
    "contracts/utils/SynthContext.sol",
    "contracts/access/Governable.sol",
    "contracts/access/Manageable.sol",
    "contracts/utils/Pauseable.sol",

    # =================================================================================
    # Core CDP: pool accounting, collateral receipts, debt issuance/interest, treasury custody
    # =================================================================================
    "contracts/Pool.sol",
    "contracts/PoolRegistry.sol",
    "contracts/DepositToken.sol",
    "contracts/DebtToken.sol",
    "contracts/SyntheticToken.sol",
    "contracts/Treasury.sol",
    "contracts/FeeProvider.sol",

    # =================================================================================
    # Leverage / flash repay and user-facing gateways
    # =================================================================================
    "contracts/SmartFarmingManager.sol",
    "contracts/NativeTokenGateway.sol",
    "contracts/VesperGateway.sol",

    # =================================================================================
    # Cross-chain synth bridging (LayerZero OFT)
    # =================================================================================
    "contracts/ProxyOFT.sol",

    # =================================================================================
    # AMO supply, rewards and airdrops
    # =================================================================================
    "contracts/AMO.sol",
    "contracts/RewardsDistributor.sol",
    "contracts/MetAirdrop.sol",
    "contracts/utils/RecurringAirdrop.sol",

    # =================================================================================
    # Shared primitives: math, account sets, reentrancy guards, token sweeping
    # =================================================================================
    "contracts/lib/WadRayMath.sol",
    "contracts/lib/MappedEnumerableSet.sol",
    "contracts/utils/ReentrancyGuardTransient.sol",
    "contracts/utils/ReentrancyGuardDeprecated.sol",
    "contracts/utils/TokenHolder.sol",

    # =================================================================================
    # Upgradeable storage layouts (versioned slots behind live proxies)
    # =================================================================================
    "contracts/storage/PoolStorage.sol",
    "contracts/storage/PoolRegistryStorage.sol",
    "contracts/storage/DepositTokenStorage.sol",
    "contracts/storage/DebtTokenStorage.sol",
    "contracts/storage/SyntheticTokenStorage.sol",
    "contracts/storage/TreasuryStorage.sol",
    "contracts/storage/FeeProviderStorage.sol",
    "contracts/storage/SmartFarmingManagerStorage.sol",
    "contracts/storage/ProxyOFTStorage.sol",
    "contracts/storage/RewardsDistributorStorage.sol",
    "contracts/storage/AMOStorage.sol",
]


target_scopes = [
    "Critical. An unprivileged caller acts as another account, because Operator.execute/getActualMsgSender, the transient MSG_SENDER slot, or SynthContext._msgSender resolves the wrong actor - a nested or re-entered call, a stale or zero slot, a contract that mixes msg.sender with _msgSender(), or a SynthContext check (onlyPool, onlyIfCanMint/Burn/Seize, onlyIfSmartFarmingManager, ProxyOFT._debitFrom from_ check, Treasury.pull) passing for the Operator - letting the attacker move a victim's synths, deposit tokens, debt, allowances, or anything left in the Operator.",
    "Critical. Collateral backing open debt leaves the Treasury, or deposit tokens are minted without matching underlying, because DepositToken deposit/withdraw/transfer/transferFrom/flashWithdraw/withdrawFrom, _revertIfLocked/unlockedBalanceOf, the Treasury balance-delta measurement, Treasury.pull, or NativeTokenGateway/VesperGateway deposit/withdraw with attacker-chosen pool_/vToken_ mis-accounts amounts, fees, or lock state, draining other users' collateral.",
    "Critical. Synthetic tokens are minted without the matching debt, or debt is erased without burning synth, because DebtToken issue/mint/flashIssue/repay/repayAll/_mint/_burn, principalOf/debtIndexOf/debtIndex interest math, accrueInterest (including the pendingInterestFee try/catch), fee quotes, or Pool.debtPositionOf/depositOf/debtOf rounding lets a borrower exceed the issuable limit or lower their debt, leaving the protocol insolvent.",
    "Critical. Pool.liquidate seizes more collateral than the repaid debt plus configured fees, liquidates a healthy position, or lets a borrower make an underwater position unliquidatable, because quoteLiquidateOut/In/Max, the maxLiquidable ratio check, debtFloorInUsd, multi-token debtOf/depositOf valuation, DepositToken.seize fee split, or accrue-before-check ordering is wrong, causing theft from borrowers or unrecoverable bad debt.",
    "Critical. An attacker extracts value from the synth system through oracle-priced paths, because Pool.swap/quoteSwapIn/quoteSwapOut, issue, leverage, or liquidation price assets with a MasterOracle quote the attacker can move in the same transaction (flash loan, vault-share donation to pricePerShare-based collateral such as vaTokens, AMM price used by the swapper), letting them mint, swap, or seize for more than they pay.",
    "Critical. SmartFarmingManager.leverage or flashRepay ends with debt not backed by the collateral deposited, pays a user from funds that are not theirs, or strands user funds, because the attacker-chosen tokenIn_, balance-delta measurement around the swapper, flashIssue then DebtToken.mint ordering, flashWithdraw without a lock check, repay-fee math, leftover synth refund, or the final debtPositionOf health check can be abused, including via token callbacks during the swap.",
    "Critical. Cross-chain synth supply is created or destroyed incorrectly, because ProxyOFT._debitFrom/_creditTo/sendFrom, the inherited OFTCore/ComposableOFTCore receive path (PT_SEND_AND_CALL, _sendAndCallAck, onOFTReceived), NonblockingLzApp retryMessage/retryOFTReceived, or the SyntheticToken totalBridgedIn/totalBridgedOut caps let an unprivileged user get credited twice, debit someone else, or have a burned transfer permanently uncreditable.",
    "High. Unclaimed rewards or airdrops are stolen, inflated, or permanently frozen, because the permissionless RewardsDistributor updateBeforeMintOrBurn/updateBeforeTransfer/claimRewards, the accountIndexOf==0 to INITIAL_INDEX fallback, DebtToken balances growing through interest without a checkpoint, the DepositToken/DebtToken transfer hooks, or the RecurringAirdrop/MetAirdrop leaf, claimed[] accounting and esMET lockFor path lets an attacker claim more than they earned or someone else's share.",
    "Critical. An unprivileged attacker permanently or temporarily freezes a victim's funds, because dust DepositToken transfers push the victim to MAX_TOKENS_PER_USER in MappedEnumerableSet, debtFloorInUsd blocks the victim's repay or exit, a reverting rewards hook or pool/registry state check sits on the withdraw, repay or liquidation path, or bridged-supply and total-supply caps can be filled so victims cannot withdraw collateral, repay debt, or receive bridged synth.",
    "Critical/High blind spot. An unprivileged user breaks an assumption Metronome never wrote down: a value checked in one contract and trusted as still valid in another in the same transaction (health, lock, supply cap, price), a check enforced on the direct path but missing on its Operator, gateway, SmartFarmingManager or bridge twin, a new ProxyOFT/Operator/transient-guard version running on storage slots written by an older layout, a shutdown or pause flag that blocks exits but not entries, or rounding that always favors the caller and can be repeated - yielding theft, insolvency, or permanently frozen funds.",
]


scope_scan = [
]


def question_generator(target_file: str) -> str:
    """
    Generate exploit-focused audit and fuzzing questions for one metronome-synth target.

    ```
    target_file format:
    "'File Name: contracts/Pool.sol -> Scope: Critical. ...'"
    """

    prompt = f"""
    ```

    Generate exploit-focused security audit questions for this exact metronome-synth target:

    {target_file}

    Project focus:
    Metronome Synth is a CDP synthetic-asset protocol (Ethereum, Optimism and other L2s). Users deposit collateral into DepositTokens (held by Treasury), issue msAssets against DebtTokens, swap synths at oracle price in Pool, get liquidated in Pool, leverage/flash-repay through SmartFarmingManager, bridge synths via ProxyOFT (LayerZero), batch calls through Operator (SynthContext resolves the real sender), and earn rewards/airdrops.

    Rules:
    * Treat `File Name:` as the exact file/contract.
    * Treat `Scope:` as the ONLY impact to target.
    * Assume full repo context is accessible.
    * Do not ask for code or say anything is missing.
    * Use exact Solidity symbols (contract, function, modifier, storage variable) when possible.
    * Attacker is unprivileged only: any EOA or attacker-deployed contract calling public/external functions directly or through Operator.execute, NativeTokenGateway or VesperGateway, using flash loans, own ERC20s/fake pools/fake vTokens passed as arguments, front-running, donations, and AMM trades.
    * Attacker is NOT governor, guardian, AMO keeper, tokenSpeedKeeper, fee collector, proxy admin, oracle operator, LayerZero endpoint/relayer/DVN, or a trusted remote ProxyOFT. Never base a question on a malicious peer, node, relayer, or bridge.
    * Out of scope, never ask about: incorrect third-party oracle data (manipulation reachable in one transaction IS in scope), governance parameter choices, centralization, external stablecoin depeg, Sybil, 51% attacks, gas/unbounded-loop/out-of-gas DoS, best practices, and issues already in the audits/ reports.
    * Ignore test files, mocks, scripts, deploy, interfaces-only, dependencies/ (unless reached through Metronome code), and config-only findings.
    * Every question must be a concrete real-world scenario reachable from a valid entry point (deposit, withdraw, issue, repay, repayAll, liquidate, swap, leverage, flashRepay, sendFrom, claimRewards, claim, gateway deposit/withdraw, Operator.execute). No generic unbounded-allocation or memory speculation.
    * Generate 40 to 80 high-signal questions.
    * At least 70% must target direct theft of user funds, protocol insolvency (unbacked synth or bad debt), or permanent freezing of funds; the rest may target theft/freezing of unclaimed yield or temporary freezing.
    * Every question must be provable with a Hardhat or Foundry mainnet-fork test.
    * Avoid generic checklist questions and repeated root causes.

    Core invariants:
    * Identity: an account's synths, deposit tokens, debt and allowances change only by that account's own call (direct or via Operator) or a rule-conforming liquidation.
    * Solvency: after any user action, debt stays within the issuable limit, every synth minted is matched by debt, AMO supply, or a bridged-in credit, and underlying leaving Treasury equals deposit tokens burned.
    * Liquidation: only unhealthy positions, repaid amount <= maxLiquidable share, seized collateral == repaid value plus configured fees.
    * Bridge conservation: each burn on the source chain is credited exactly once on the destination.
    * Rewards: accrued = balance x index delta since the account's last checkpoint, claimed once.
    * Liveness: when not shut down, users can always repay, withdraw unlocked collateral, and exit.

    Each question must include:
    1. target contract/function;
    2. attacker action (concrete calls, arguments, contracts deployed);
    3. preconditions (pool state, victim position, fees, flags);
    4. execution sequence;
    5. invariant tested;
    6. scoped impact;
    7. proof idea.

    Output only valid Python. No markdown. No explanations.

    questions = [
    "[File: {target_file}] [Function: contract.function] Can an unprivileged ATTACKER_ACTION under PRECONDITIONS trigger EXECUTION_SEQUENCE, violating INVARIANT, causing scoped impact: SCOPE_IMPACT? Proof idea: fork test PARAMETERS and assert IDENTITY, SOLVENCY, LIQUIDATION, BRIDGE_CONSERVATION, REWARDS, or LIVENESS.",
    ]
    """
    return prompt


def audit_format(security_question: str) -> str:
    """
    Generate a focused metronome-synth exploit-validation prompt.
    """

    prompt = f"""# SECURITY AUDIT PROMPT

## Question
{security_question}

## Rules
- Use existing repo context only. Analyze only this question and scoped impact.
- Attacker is unprivileged only: any EOA or attacker-deployed contract using public entry points directly or via Operator/gateways, flash loans, own tokens/fake pools/fake vTokens as arguments, front-running, donations, and AMM trades.
- Attacker is not governor, guardian, keeper, fee collector, proxy admin, oracle operator, or LayerZero endpoint/relayer/trusted remote. Reject malicious-peer, malicious-node, and malicious-bridge premises.
- Reject incorrect third-party oracle data (same-transaction manipulation is allowed), governance parameter choices, centralization, stablecoin depeg, Sybil, gas/unbounded-loop DoS, best practices, and issues already in audits/.
- Reject test/mock/script/deploy/dependencies-only and config-only findings.
- Focus on real impact: direct theft of user funds, protocol insolvency, permanent freezing of funds, theft or freezing of unclaimed yield, or temporary freezing of funds.

## Validate
- Trace the exact reachable path from a public entry point (deposit, withdraw, issue, repay, liquidate, swap, leverage, flashRepay, sendFrom, claimRewards, claim, gateway, Operator.execute) into the affected function.
- Check whether modifiers, _revertIfLocked, the final health check, supply caps, nonReentrant, SynthContext sender checks, or pause/shutdown flags already stop it.
- Confirm it works against the current deployed configuration, not only with an unusual governor setting.
- Accept only a concrete loss, insolvency, or freeze with a quantified amount.
- Require exact file/function support and a reproducible Hardhat or Foundry fork PoC.

## Output
If valid, output exactly:

### Title
[Bug statement] - ([File: file_path])

### Summary
[2-3 sentences]

### Finding Description
[Code path, root cause, attacker inputs, exploit flow, and why existing checks fail]

### Impact Explanation
[Concrete scoped impact and matching category: Theft of User Funds, Protocol Insolvency, Permanent Freezing of Funds, Theft/Freezing of Unclaimed Yield, or Temporary Freezing of Funds]

### Likelihood Explanation
[Preconditions, attacker cost, feasibility, repeatability]

### Recommendation
[Specific fix]

### Proof of Concept
[Fork test plan with expected assertions]

If invalid, output exactly:
#NoVulnerability found for this question.

No extra text.
"""
    return prompt


def validation_format(report: str) -> str:
    """
    Generate a strict Immunefi-style validation prompt for metronome-synth security claims.
    """
    prompt = f"""# VALIDATION PROMPT

## Security Claim
{report}

## Rules
- Validate only the submitted claim.
- Check SECURITY.md and Researcher.Md for scope, exclusions, and valid impact classes.
- Do not create a new vulnerability if the submitted claim is weak or invalid.
- Do not upgrade severity unless the provided evidence proves the higher impact.
- Accepted severities (Immunefi smart contract program): Critical, High, and Medium. Reject Low, informational, hardening, and best-practice submissions.
- Critical: direct theft of any user funds at rest or in motion (other than unclaimed yield), protocol insolvency, or permanent freezing of funds.
- High: theft or permanent freezing of unclaimed yield (RewardsDistributor, MetAirdrop), or temporary freezing of funds.
- Medium: contract fails to deliver promised returns without losing value, or griefing that damages users or the protocol with no profit to the attacker.
- Reject if the exploit needs governor, guardian, keeper, fee collector, proxy admin, or oracle operator privileges, leaked keys, victim social engineering, or a malicious LayerZero endpoint, relayer, DVN, trusted remote, peer, or node.
- Reject incorrect third-party oracle data, external stablecoin depeg not caused by the bug, governance/economic 51% attacks, Sybil, lack of liquidity, centralization risk, gas/unbounded-loop DoS, and DoS against project assets.
- Reject third-party dependency bugs not reachable through Metronome code, docs/style, and test/mock/script/deploy/config-only issues.
- Reject if already reported, exploited by the reporter, or listed in the audits/ reports.
- A valid report must be triggerable by an unprivileged user through a public entry point, unless the claim proves escalation from that starting point.
- Prefer #NoVulnerability over speculative reports.

## Required Validation Checks
All must pass:
1. Exact in-scope file, contract, function, and line references.
2. Clear root cause and broken security assumption.
3. Reachable exploit path: preconditions -> attacker calls -> trigger -> loss, insolvency, or freeze.
4. Modifiers, lock checks, health checks, supply caps, reentrancy guards, SynthContext sender checks, and pause/shutdown flags reviewed and shown insufficient.
5. Concrete in-scope impact with a quantified amount, realistic likelihood, and correct severity.
6. Reproducible Hardhat or Foundry fork PoC (mandatory per program rules; local fork only).
7. No obvious rejection reason from SECURITY.md, known issues, privilege assumptions, or scope exclusions.

## Silent Triage Questions
Before output, internally answer:
- Can an unprivileged user trigger this through a public entry point?
- Does the code actually behave as claimed on the deployed version and configuration?
- Is the loss caused by this code, not by oracle error, governance choice, or victim mistake?
- Is the theft, insolvency, or freeze concrete and quantified rather than hypothetical?
- Would the Metronome Immunefi triager accept the PoC?
- What exact test would prove it?

## Output
If valid, output exactly:

Audit Report

## Title
[Clear vulnerability statement] - ([File: file_path])

## Summary
[2-3 sentence summary of the bug and impact]

## Finding Description
[Exact code path, root cause, exploit flow, and why existing checks fail]

## Impact Explanation
[Concrete in-scope impact, severity rationale, and Immunefi impact category]

## Likelihood Explanation
[Attacker capability, capital needed, feasibility, repeatability]

## Recommendation
[Specific fix guidance]

## Proof of Concept
[Minimal reproducible steps or fork test plan]

If invalid, output exactly:
#NoVulnerability found for this question.

Output only one of the two outcomes above. No extra text.
"""
    return prompt


def scan_format(report: str) -> str:
    """
    Generate a short cross-project analog scan prompt for metronome-synth.
    """
    prompt = f"""# ANALOG SCAN PROMPT

## External Report
{report}

## Rules
- Use in-scope production repo context only. Do not ask for code or claim missing files.
- Use the external report only as a bug-class hint, not as proof. The analog must stand on Metronome's own code.
- Attacker is unprivileged only: any EOA or attacker-deployed contract using public entry points directly or via Operator.execute/gateways, flash loans, own tokens/fake pools/fake vTokens as arguments, front-running, donations, and AMM trades.
- Reject analogs needing governor, guardian, keeper, fee collector, proxy admin, oracle operator, or a malicious LayerZero endpoint/relayer/DVN/trusted remote, peer, or node.
- Reject incorrect third-party oracle data (same-transaction manipulation is allowed), governance parameter choices, centralization, stablecoin depeg, Sybil, gas/unbounded-loop DoS, best practices, mocked-only paths, known audits/ issues, or no impact.
- Ignore test/mock/script/deploy/dependencies-only and config-only code.

## Map the Bug Class
Pick the strongest reachable Metronome surface for this class, then name the exact contract and function:
- Meta-sender / multicall / trusted forwarder: Operator.execute, getActualMsgSender, transient MSG_SENDER, SynthContext._msgSender and every modifier built on it (onlyPool, onlyIfCanMint/Burn/Seize, onlyIfSmartFarmingManager), RecurringAirdrop using msg.sender.
- Lending collateral / vault shares / fee-on-transfer: DepositToken deposit/withdraw/transfer/transferFrom, _revertIfLocked, unlockedBalanceOf, Treasury balance delta and pull, NativeTokenGateway, VesperGateway (attacker-chosen pool_/vToken_).
- Borrow / interest index / debt shares: DebtToken issue/mint/flashIssue/repay/repayAll, principalOf, debtIndexOf, accrueInterest, pendingInterestFee, Pool.debtPositionOf/depositOf/debtOf.
- Liquidation math / bad debt: Pool.liquidate, quoteLiquidateOut/In/Max, maxLiquidable, debtFloorInUsd, DepositToken.seize fee split.
- Oracle-priced swaps / price manipulation: Pool.swap, quoteSwapIn/Out, FeeProvider.swapFees, MasterOracle quotes of vault-share collateral, SmartFarmingManager._calculateLeverageDebtAmount.
- Flash loans / leverage / zaps with external swapper: SmartFarmingManager.leverage and flashRepay, arbitrary tokenIn_, balance-delta swaps, leftover refunds, final health check, token callbacks.
- Bridge / OFT: ProxyOFT._debitFrom/_creditTo/sendFrom, OFTCore/ComposableOFTCore receive and PT_SEND_AND_CALL, retryMessage/retryOFTReceived, SyntheticToken bridged-in/out caps.
- Rewards index / staking checkpoints: RewardsDistributor updateBeforeMintOrBurn/updateBeforeTransfer/claimRewards, accountIndexOf fallback, DebtToken interest-growing balances.
- Merkle airdrop: RecurringAirdrop.claim leaf encoding, claimed[] across root updates, MetAirdrop esMET lockFor.
- Rounding / decimals / precision: WadRayMath half-up wadMul/wadDiv, fee quotes in/out, non-18-decimal collateral, repeatable rounding in the caller's favor.
- Reentrancy / cross-contract state: ReentrancyGuardTransient per-contract slots, read-only reentrancy on debtPositionOf during token or ETH callbacks, cross-contract calls between Pool, DepositToken, DebtToken, Treasury.
- Account list / freeze griefing: MappedEnumerableSet, MAX_TOKENS_PER_USER via dust transfers, pause/shutdown flags blocking exits.
- Upgradeable storage: versioned storage layouts, initializers, and gaps across upgrades.

## Validate
- Trace the analog from a public entry point with concrete attacker calls and arguments into the named function.
- Show which invariant breaks: identity, solvency, liquidation bounds, bridge conservation, reward accrual, or liveness.
- Confirm modifiers, lock/health checks, supply caps, reentrancy guards, SynthContext checks, and pause flags do not already stop it on the deployed configuration.
- Accept only direct theft of user funds, protocol insolvency, permanent freezing of funds, theft/freezing of unclaimed yield, or temporary freezing of funds.
- Require a reproducible Hardhat or Foundry fork proof.

## Output (Strict)
If valid analog exists, output:

### Title
[Clear vulnerability statement] - ([File: file_path])

### Summary
### Finding Description
### Impact Explanation
### Likelihood Explanation
### Recommendation
### Proof of Concept

If not, output exactly:
#NoVulnerability found for this question.

No extra text.
"""
    return prompt
