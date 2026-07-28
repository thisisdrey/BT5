# Q3861: Smart account deployment and initialization: init arrays / address confusion / one-shot configure

## Question
Can an unprivileged deployer or external caller with no guardian or admin powers enter through `SmartAccountFactory.deploySmartAccount(...) and predictSmartAccountAddress(...)` with delegates, currencies, NFT collections, trusted recipients, owners, and threshold in the deployment initializer while a second configuration attempt is made against a Safe that already ran initialization once and make predicted-address semantics diverge from the actual deployed account or from who effectively controls it, breaking the rule that `configureSmartAccount` should run at most once and only in the intended delegatecall context of a fresh Safe and leading to Theft or redirection of value sent to a predicted or freshly deployed smart account?

## Target
- File/function: contracts/SmartAccountFactory.sol / deploySmartAccount, configureSmartAccount, predictSmartAccountAddress
- Entrypoint: SmartAccountFactory.deploySmartAccount(...) and predictSmartAccountAddress(...)
- Attacker controls: delegates, currencies, NFT collections, trusted recipients, owners, and threshold in the deployment initializer
- Exploit idea: make predicted-address semantics diverge from the actual deployed account or from who effectively controls it
- Invariant to test: `configureSmartAccount` should run at most once and only in the intended delegatecall context of a fresh Safe
- Expected Immunefi impact: Theft or redirection of value sent to a predicted or freshly deployed smart account
- Fast validation: Forge test repeated delegatecall attempts into `configureSmartAccount` and assert the one-shot guard holds in every reachable context.
