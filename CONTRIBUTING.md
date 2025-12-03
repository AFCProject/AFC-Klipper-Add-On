
# Contributing to This Project

Thanks for your interest in contributing! Whether it's a bug report, new feature, or documentation fix, your help is welcome and appreciated.

## Getting Started

1. **Fork the repository** and create your branch from `main` or the default branch.
2. Make your changes, following the guidelines below.
3. Submit a [pull request](https://github.com/ArmoredTurtle/AFC-Klipper-Add-On/pulls).

**NOTE** All pull requests should be up to date with the latest changes from the `DEV` branch and PRs should be opened against the `DEV` branch.

## Guidelines

### Code Style

- Python code should follow [PEP 8](https://peps.python.org/pep-0008/) as closely as possible.
- Use meaningful commit messages.
- Keep changes focused—avoid mixing unrelated fixes or features.

### Scripts

- Bash scripts should be POSIX-compliant where practical.
- Include a `#!/bin/bash` or `#!/usr/bin/env bash` shebang as appropriate.
- Add inline comments to explain non-obvious logic.

### Testing

- If applicable, test your changes with your Klipper environment.
- Make sure your code doesn't introduce errors or break existing functionality.
- While formal unit tests may not be in place yet, please validate your changes manually.

### Pull Requests

- Clearly describe what your PR does and why it’s useful.
- Link to any relevant issues.
- Smaller PRs are easier to review and merge—break large changes into smaller steps if you can.

## Bug Reports & Feature Requests

- Use GitHub [Issues](https://github.com/ArmoredTurtle/AFC-Klipper-Add-On/issues) to report bugs or request features.
- When reporting a bug, please include:
  - A description of the issue
  - Steps to reproduce
  - Any relevant logs or configuration
  
## Virtual environment

It is recommended to set up a python virtual environment to keep installed dependencies isolated from your system dependencies.

You can do this by running the following:

```shell
# Install virtualenv globally
pip install virtualenv

# Create local virtual environment (will create directory named "venv" in your working directory)
python3 -m venv venv

# Activate the virtual environment in your current terminal/shell
source venv/bin/activate
```

## Install dependencies

```shell
pip install -r requirements.txt
```

> [!NOTE]
> Klipper aims to be dependency free. We are ONLY using dependencies here for the development environment.
> We should not use any of these dependencies in any of the code that we intend to run in klipper.

### Klipper sources

This is not strictly required, but it can be helpful for your IDE (specifically tested with VSCode) to include the klipper
sources in your `PYTHONPATH`. To do this, at this time you'll need to manually clone klipper into this project like this:

```shell
git clone --depth=1 https://github.com/Klipper3d/klipper
```

Then you'll need to ensure the `klipper/klippy` is added in the `PYTHONPATH` environment variable. The project `.vscode/settings.json`
should configure that for VSCode, but you may need to refer to your own IDE for help ensuring the extra path is added.

## Linting

```shell
ruff check .
```

> [!TIP]
> Some lint errors may be automatically fixable. Run `ruff check --fix` to fix them.

## Updating `requirements.txt`

If you need to add additional development tools (e.g. `pip install <some-dependency>`) that are to be used
by CI/CD workflows, ensure they are added to the `requirements.txt` by running the following:

```shell
pip freeze > requirements.txt
```

> [!WARNING]
> Ensure this is only done from a virtual environment, otherwise it will add all of your globally installed
> dependencies.
