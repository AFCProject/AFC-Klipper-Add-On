# AI Usage Policy

AFC-Klipper-Add-On welcomes contributions from everyone, including contributions created with the help of AI tools such as GitHub Copilot, Cursor, Claude, ChatGPT, and similar assistants. This page describes what we expect from contributors who use these tools.

## Using AI for Code

It's OK to use AI to generate code, draft tests, fix bugs, or help you navigate `extras/`, `tests/`, or the Mainsail/Fluidd frontends. AI is a tool, and when it helps you ship a good change, we're happy to have it.

Regardless of whether the code is written by you or by an AI, it still has to meet the project's standards:

- It must pass `ruff check .` (see [Linting](CONTRIBUTING.md#linting) in the main contributing guide).
- Relevant tests must pass: unit tests in `tests/test_AFC_*.py` and, where applicable, klippy integration tests in `tests/klippy/*.test`. See [Running Tests](CONTRIBUTING.md#running-tests).
- New logic added to `extras/` should come with corresponding test coverage, not just a passing test suite.
- Python code should follow [PEP 8](https://peps.python.org/pep-0008/) as closely as possible.

> [!WARNING]
> Klipper aims to be dependency-free. Code that runs inside the `klippy` process (anything under `extras/`) must not have AI introduce new runtime dependencies. Extra dependencies belong only in `requirements.txt` for the development/CI environment, never in code that ships to a printer.

## Interacting with Maintainers Should Be Human

When it comes to interacting with the project (PR descriptions, code review replies, issue comments, and discussion threads), we expect to interact with real humans, not AI-generated replies.

Please do not:

- Paste a reviewer's comment back into an AI and post the raw output as your reply.
- Generate issue or PR descriptions wholesale from AI without reading and editing them yourself.
- Use AI to argue with maintainers on your behalf.
- Have AI make the commits for you.

Maintainer bandwidth is limited, and the conversation around a change (especially around hardware behavior, calibration edge cases, and Klipper/Kalico compatibility) is where most of the value of code review lives. If that conversation is between an AI on one side and a human on the other, it stops being useful.

## Disclose When AI Was Used

If AI was used to generate a significant portion of an issue, PR, or the code it contains, please say so in the submission. A short note in the PR description is enough, for example: "The initial implementation was drafted with Claude and then reviewed, tested, and edited by me."

Issues and pull requests that appear to be AI-generated but don't disclose it may be closed without review. Contributors who repeatedly submit undisclosed AI content, or who ignore this policy, may be blocked from contributing.

## Quality Over Quantity

Modern AI tools make it easy to generate a large number of changes very quickly. Please resist the temptation to open many pull requests at once, for example by pointing an AI tool at the codebase and submitting whatever it produces.

A stack of simultaneous, similar pull requests from one author takes much longer for us to review than a single, well-tested change, and it's often a sign that the work hasn't been read or tested by a human, or run against real hardware. We would much rather receive one change that you understand and have verified than ten that you haven't.

A good rhythm is to open one pull request, work with us to get it reviewed and merged against `DEV`, and only then open the next one. Pull requests that are low-effort, untested, or undisclosed AI output may be closed without a detailed review, and authors who repeatedly submit them may be blocked from contributing.

## You Are Responsible for What You Submit

Before you open an issue or a PR, you should:

- **Understand the code.** Read what the AI produced. Be able to explain what each change does and why it's needed, especially anything touching lane state, calibration, or homing logic, where a subtle bug can cause a crash or hardware damage.
- **Verify it works.** Run `ruff check .`, run `python -m pytest tests/` (and `./run-tests.sh` for klippy integration coverage), and where possible confirm the behavior on real or simulated hardware.
- **Check coverage on new logic.** Run `python -m pytest tests/ --cov=extras --cov-report=term-missing --cov-report=html:coverage-report --cov-branch -k test_` and look at the report to confirm the lines your AI-assisted change added or modified are actually exercised, not just that the suite passes.
- **Write the PR description yourself.** Don't let AI generate your PR description wholesale. It should be written by you, following our default PR template, and accurately describe what the code actually does. AI-generated descriptions tend to be long, repetitive, or inaccurate, and a templated, human-written description is what lets maintainers review efficiently.

You are the author of the contribution. The AI is not.

## AI Agents
This page is for human contributors who are using AI as an assistant. Autonomous AI agents operating directly on this repository follow a separate set of rules. Those rules live in `AGENTS.md` file

---

*This policy is adapted from the [Actual Budget AI Usage Policy](https://actualbudget.org/docs/contributing/ai-usage-policy/), used here with attribution and modified for AFC-Klipper-Add-On's tooling, branch, and testing conventions.*

*Disclosure: this document was drafted with Claude and then reviewed and edited by a human, in keeping with the disclosure expectations described above.*
