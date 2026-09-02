# AGENTS.md

## Purpose

This repository contains user-facing tooling that helps owners of an Elegoo Centauri Carbon 2 (CC2) 3D printer prevent or recover a failure of the stock camera.

Affected cameras have a firmware and flash-layout combination on which JFFS2 erase or garbage collection does not work correctly. The stock startup process copies several configuration files on every boot. Obsolete records accumulate, and after roughly 30–40 power cycles the configuration partition may fill up and the camera may stop booting successfully.

Treat this as hardware-recovery software. A mistake can destroy the only usable copy of a camera's device-specific data or leave the camera unbootable.

## Priorities

When priorities conflict, use this order:

1. Protect the user's hardware and device-specific data.
2. Fail closed when identity, firmware, layout, transport state, or verification is uncertain.
3. Give first-time users a clear path to diagnosis, prevention, recovery, and verification.
4. Keep implementation and research auditable.
5. Prefer convenience only when it does not weaken the safeguards above.

Do not weaken validation, confirmation, backup, identity, or readback requirements merely to support another firmware dump or shorten a workflow.

## Human decisions

Product-level decisions are made by a human developer, not autonomously by an agent. These include decisions about:

- supported users, devices, firmware, and workflows;
- user-facing scope, behavior, defaults, and terminology;
- CLI and public API contracts;
- safety policy and accepted risk;
- compatibility promises and file formats;
- redistribution of sensitive or third-party material.

Agents should identify these decisions, research the available options, and recommend a course of action during an interactive session. Explain the tradeoffs and wait for an explicit developer decision before implementing one of the options. An explicit task or accepted review suggestion counts as a developer decision; general permission to work on the project does not.

## User-facing content

README files, CLI help, console output, and docstrings for shared or public APIs are user-facing. Write them for first-time visitors with no prior knowledge of the tools.

A first-time visitor should be able to determine:

- whether this repository applies to their printer and camera;
- what the camera failure is, without unnecessary implementation detail;
- whether their camera may be affected;
- how to prevent the failure while the camera still works;
- how to recover an already failed camera;
- what hardware, software, access, and skill level a procedure requires;
- how to obtain persistent ADB access and, when relevant, which USB, UART, or SPI pins and external hardware to use;
- which steps are read-only and which can modify or erase the camera;
- how to verify the result and what to do when a check fails.

Put prerequisites and safety warnings before the action they constrain. Use exact commands, expected outcomes, and explicit stop conditions. Prefer plain language and define necessary technical terms on first use.

Do not add a project-structure overview while the CLI tools and their READMEs are being consolidated. Describe supported user workflows and stable contracts rather than a transient directory layout.

Keep user-facing documentation aligned with the current snapshot. Except in a `CHANGELOG`, `MIGRATION_GUIDE`, PR description, or commit message, do not:

- describe what changed in a PR;
- contrast current behavior with an earlier implementation;
- explain how an old file format differs from its replacement;
- retain historical commentary that does not help users work with the current version.

Users need to know what is true and what to do now. They do not need a narrative of how the project reached its current state.

Existing documents do not need to be rewritten as part of unrelated work. Avoid introducing new historical commentary, and leave broader cleanup for a dedicated task.

## Research and developer documentation

Research files and research directories are also user-facing, but their audience consists of power users who may want deep technical information about subjects such as the camera's HID update mechanism, communication protocol, flash layout, or firmware behavior.

In research material:

- separate directly observed facts from inference;
- state what evidence supports a claim;
- record exact offsets, lengths, hashes, protocol bytes, and reproducible read-only methods where useful;
- label assumptions, unknowns, and hardware-unverified behavior explicitly;
- distinguish independently reconstructed or normalized pseudocode from vendor-authored source.

Research documents may be detailed, but they should remain navigable and practical.

Other Markdown files may contain development guidance primarily intended for agents when that information does not belong in user or research documentation. They must not contradict tests, safety invariants, or current user-facing guidance.

## Safety invariants

Preserve these properties in code, tests, documentation, and examples:

- Inspection, planning, validation, and backup operations must not silently perform persistent writes.
- A read-only command must remain read-only. If a prerequisite requires mutation, expose it as a separate, explicit operation.
- Never use another camera's dump as a generic recovery image. Preserve and validate the current camera's identity-bearing data.
- Require trustworthy backups before destructive work.
- Do not reduce the established stable-read requirement without documented evidence, an explicit risk decision made by a developer, not an agent, and regression tests.
- Validate device identity, firmware invariants, flash size and layout, partition map, hashes, archive structure, and allowed change regions before a write.
- Refuse ambiguous devices, unsupported firmware or layouts, wrong-unit data, unstable reads, malformed inputs, unexpected protocol states, timeouts, and verification mismatches.
- Destructive operations require specific, informed consent. Consent for one operation must not imply consent for a separate mutation.
- Validate and plan locally before opening a hardware transport whenever possible.
- After a write, require the strongest practical readback and byte-level verification before claiming success.
- Bound input sizes, decompression, retries, subprocesses, and device waits. Treat dumps, archives, manifests, and device responses as untrusted input.
- Do not add a generic force path around firmware, identity, layout, or write-region validation failures. A refusal is a safety feature.
- Do not broaden supported firmware families, flash layouts, identities, commands, or writable regions without evidence and regression coverage.

An agent may propose a narrow risk exception, but only a developer may approve it. The remaining risk must be clearly explained, explicitly accepted, recorded in the result, and covered by tests. The exception must not bypass unrelated invariants.

Never describe static analysis, mocks, synthetic vectors, or offline tests as physical-hardware validation. State separately:

- what has been verified in software;
- what has been observed on physical hardware;
- what remains hardware-unverified.

## Tests and contracts

Tests are the primary source of truth for behavioral contracts. Markdown files may provide additional rationale and research evidence, but they do not override tested behavior.

For every behavior change:

- add or update focused regression tests;
- cover success, refusal, and no-side-effect paths;
- verify that failures occur before USB, HID, ADB, or write access when that ordering is part of the safety contract;
- use mocks, synthetic byte strings, generated archives, and minimal protocol vectors for committed automated tests;
- run the relevant offline tests and, when practical, the complete offline suite;
- keep CLI help, console messages, shared-library docstrings, and README instructions consistent with the tested behavior.

Committed automated tests must not require a physical camera or perform real destructive hardware operations by default.

An agent may be given real camera dumps outside the repository for local verification or manual testing. These dumps may be used in the provided development environment, but they must remain outside the repository and must not be committed.

The procedure and results of a manual test may be documented so that the test can be repeated later. Record the relevant setup, commands, expected behavior, observed behavior, hashes, and hardware-validation status without embedding or redistributing the dump itself.

An agent may also ask the developer to perform a test or verification on physical hardware. Such a request must:

- explain the purpose of the test;
- provide exact steps and expected observations;
- identify any destructive or persistent action before it occurs;
- include clear stop conditions;
- avoid claiming success until the developer reports the result.

## Redistribution and test data

Never add full memory, flash, ROM, disk, or firmware images to the repository.

This prohibition includes:

- complete or substantially complete camera dumps;
- backup archives containing dumps;
- vendor firmware packages;
- extracted proprietary binaries or files;
- third-party material whose redistribution rights are absent or uncertain.

This applies to documentation, examples, fixtures, tests, commits, and branches.

Use hashes, fingerprints, offsets, sizes, schemas, structural metadata, synthetic fixtures, and independently written descriptions or implementations instead.

Factual interoperability information may be documented. This includes communication protocols, command values, flash layouts, known credentials relevant to using or researching the camera, and independently written reverse-engineered pseudocode. Clearly distinguish facts, inference, and reconstruction from vendor-authored material.

Keep user-supplied dumps outside the repository. Never add them to examples, tests, or documentation.

Agents must not independently upload or attach dumps, firmware, user-provided files, or other sensitive material to issues, PRs, releases, CI artifacts, external services, or other shared locations.

If sharing such a file is considered necessary, explain why and leave the decision and the actual upload to the developer. Developer approval does not permit an agent to perform the upload on the developer's behalf.

## Review checklist

Before finishing a change, verify that:

- user-facing instructions lead with diagnosis and the safest applicable workflow;
- read-only and mutating actions remain clearly separated;
- failure paths stop safely and preserve the user's backup;
- claims match the available evidence and hardware-validation status;
- tests cover the contract, including refusals and absence of side effects;
- documentation describes the current state rather than the history of the change;
- product-level and risk decisions were made explicitly by a developer;
- no device dump, proprietary firmware material, or other unlicensed third-party material was committed;
- no sensitive file was independently uploaded or attached.
