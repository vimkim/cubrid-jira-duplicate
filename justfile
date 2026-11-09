# Run recipes from this checkout; pass arguments as quoted shell parameters.
set positional-arguments

# Show available commands.
default:
    @just --list

# Prepare the development environment without changing the global command.
setup:
    uv sync

# Run offline tests; accepts pytest options: just test -k cache.
test *args:
    uv run pytest "$@"

# Lint, then run the offline tests.
check:
    uv run ruff check
    uv run pytest

# Install the cubrid-jira-dup command from this checkout and verify that it starts.
install:
    uv tool install --reinstall .
    @"$(uv tool dir --bin)/cubrid-jira-dup" --help >/dev/null

# Refresh the user command from local source for daily-update-cubrid.
sync: install
