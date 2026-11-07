# Repository Instructions

## Sensitive Configuration

- Store credentials and private infrastructure identifiers in Infisical and reference them using the existing `vault://` pattern. Do not hard-code them in tracked files, logs, tests, or examples.
- Before adding or changing configuration, inspect related files and changes for exposed sensitive values; verify that secret resolution works without printing values.

## Command Names

- Keep `just` recipe names to a single simple, memorable word or term; do not use hyphenated recipe names.
- Prefer conversational commands such as `just update plex` when an existing verb naturally covers the action. A standalone command such as `just status` is also appropriate; `just verb noun` is not mandatory.
- Preserve existing command behavior when adding subcommands.

## Headings

- Use title case for every Markdown and MDX heading, page title, and component title: capitalize major words; lowercase articles, coordinating conjunctions, and short prepositions unless first or last ("Back Up the Databases", "Pools on nexus").
- Keep hostnames, commands, and other identifiers in their literal case.
