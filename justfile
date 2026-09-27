set shell := ["bash", "-euo", "pipefail", "-c"]
set positional-arguments

subprojects := "docs web"
# Subprojects plus the Worker, which has dependencies and lint but no fmt or test recipes.
packages := subprojects + " cloudflare"
tools := "git just node npm bun curl unzip python3 magick pre-commit mint wrangler"
# pre-commit hooks that rewrite files; `lint` skips them and `fmt` runs only them.
fixers := "trailing-whitespace end-of-file-fixer mixed-line-ending shfmt prettier"
image_paths := "docs web"

# List available recipes.
default:
    @{{ just_executable() }} --list

# Check that required tools are on PATH.
doctor:
    #!/usr/bin/env bash
    set -euo pipefail
    missing=()
    for tool in {{ tools }}; do command -v "$tool" >/dev/null || missing+=("$tool"); done
    if (( ${#missing[@]} )); then
        echo "missing: ${missing[*]}" >&2
        echo "install with: brew install just node oven-sh/bun/bun imagemagick pre-commit cloudflare-wrangler && npm install --global mint" >&2
        exit 1
    fi
    echo "all tools present"

# Initialize missing submodules, install Git hooks, and install project dependencies.
setup: doctor
    git submodule status | awk '/^-/ { print $2 }' | while read -r dir; do git submodule update --init -- "$dir"; done
    pre-commit install --install-hooks --hook-type pre-commit --hook-type commit-msg
    for p in {{ packages }}; do (cd "$p" && {{ just_executable() }} install); done

# Update pre-commit hook revisions and project dependencies.
update:
    pre-commit autoupdate
    for p in {{ packages }}; do (cd "$p" && {{ just_executable() }} update); done

# Run every non-mutating check across the repository and subprojects.
lint: images-check
    SKIP="{{ replace(fixers, ' ', ',') }}" pre-commit run --all-files
    for p in {{ packages }}; do (cd "$p" && {{ just_executable() }} lint); done

# Apply every formatter across the repository and subprojects.
fmt:
    #!/usr/bin/env bash
    set -euo pipefail
    # A fixer exits 1 after changing files; a second failure is a real error.
    for hook in {{ fixers }}; do
        pre-commit run "$hook" --all-files >/dev/null || pre-commit run "$hook" --all-files
    done
    for p in {{ subprojects }}; do (cd "$p" && {{ just_executable() }} fmt); done

# Run subproject tests.
test:
    for p in {{ subprojects }}; do (cd "$p" && {{ just_executable() }} test); done

# Convert rasters the docs and web sites reference to WebP, update those references, and delete the originals.
images *paths: && images-check
    #!/usr/bin/env bash
    set -euo pipefail
    if (( $# )); then
        exec scripts/images.py convert --base "{{ invocation_directory() }}" "$@"
    fi
    exec scripts/images.py convert {{ image_paths }}

# Check that image files match their extension and references use the existing format.
images-check:
    scripts/images.py check

# Run a docs recipe, e.g. `just docs dev`.
docs *args:
    cd docs && {{ just_executable() }} "$@"

# Run a web recipe, e.g. `just web deploy`.
web *args:
    cd web && {{ just_executable() }} "$@"

# Run a Cloudflare Worker recipe, e.g. `just cf deploy`.
cf *args:
    cd cloudflare && {{ just_executable() }} "$@"
