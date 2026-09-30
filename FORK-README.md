# uxiner/Smart_Group_Bot

Personal fork of [Hamster-Prime/Smart_Group_Bot](https://github.com/Hamster-Prime/Smart_Group_Bot),
kept as the source of truth for one private group deployment.

- `main` tracks this fork's own line: upstream `main` plus local customizations.
- `upstream` is the original repository. Updates are pulled with
  `git fetch upstream && git merge upstream/main` and conflicts are resolved here.
- **No pull requests are opened against upstream.** This branch is not intended to be
  merged back; it is a deployment branch.
- Secrets are never committed: the bot reads them from an untracked `.env`
  (`config.toml` holds no credentials).
